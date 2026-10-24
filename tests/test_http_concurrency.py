"""Genuinely simultaneous HTTP requests against a real server (needs TEST_DATABASE_URL)."""
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

from tests.conftest import make_user, scalar

CUSTOMER = {"name": "Load Test", "email": "load@example.com"}


def sign_in(base, pool, role="staff"):
    user = make_user(pool, role)
    r = httpx.post(f"{base}/auth/login", json={"email": user["email"], "password": user["password"]}, timeout=30)
    assert r.status_code == 200, r.text
    return httpx.Client(base_url=base, timeout=60, headers={"Authorization": f"Bearer {r.json()['access_token']}"},
                        limits=httpx.Limits(max_connections=100))


def send(client, method, url, retries=12, **kwargs):
    """Like a careful real client: back off and retry when the server says 'busy' (503) or 'slow down' (429)."""
    for _ in range(retries):
        r = client.request(method, url, **kwargs)
        if r.status_code not in (503, 429):
            return r
        time.sleep(float(r.headers.get("retry-after", 1)) * 0.2 + 0.05)
    return r


def order(client, product_id, qty=1, key=None):
    return send(client, "POST", "/orders", headers={"Idempotency-Key": key or str(uuid.uuid4())},
                json={"customer": CUSTOMER, "items": [{"product_id": product_id, "quantity": qty}]})


def test_forty_simultaneous_buyers_cannot_oversell_ten_units(live_server, pool):
    client = sign_in(live_server, pool)
    product = client.post("/products", json={"sku": "HOT-1", "name": "Hot item", "price": "9.99", "stock": 10}).json()
    barrier = threading.Barrier(40)

    def buy(_):
        barrier.wait()
        return order(client, product["id"]).status_code

    with ThreadPoolExecutor(max_workers=40) as pool_:
        codes = Counter(pool_.map(buy, range(40)))
    assert codes[201] == 10 and codes[409] == 30 and set(codes) == {201, 409}, codes
    assert client.get(f"/products/{product['id']}/stock").json()["stock"] == 0
    assert scalar(pool, "SELECT count(*) FROM orders") == 10
    admin = sign_in(live_server, pool, "admin")
    assert admin.get("/inventory/audit").json()["consistent"] is True


def test_twenty_simultaneous_duplicates_make_one_order(live_server, pool):
    client = sign_in(live_server, pool)
    product = client.post("/products", json={"sku": "DUP-1", "name": "Dup item", "price": "5.00", "stock": 50}).json()
    key, barrier = str(uuid.uuid4()), threading.Barrier(20)

    def submit(_):
        barrier.wait()
        r = order(client, product["id"], qty=2, key=key)
        return r.status_code, r.json().get("id"), r.headers.get("idempotent-replay")

    with ThreadPoolExecutor(max_workers=20) as pool_:
        results = list(pool_.map(submit, range(20)))
    assert Counter(code for code, _, _ in results) == {201: 1, 200: 19}
    assert len({order_id for _, order_id, _ in results}) == 1
    assert all(replay == "true" for code, _, replay in results if code == 200)
    assert client.get(f"/products/{product['id']}/stock").json()["stock"] == 48          # deducted once
    assert scalar(pool, "SELECT count(*) FROM orders") == 1


def test_ten_simultaneous_cancels_return_the_stock_exactly_once(live_server, pool):
    client = sign_in(live_server, pool)
    product = client.post("/products", json={"sku": "CAN-1", "name": "Cancel item", "price": "5.00", "stock": 10}).json()
    placed = order(client, product["id"], qty=4).json()
    assert client.get(f"/products/{product['id']}/stock").json()["stock"] == 6
    barrier = threading.Barrier(10)

    def cancel(_):
        barrier.wait()
        return send(client, "POST", f"/orders/{placed['id']}/cancel", json={"reason": "race"}).status_code

    with ThreadPoolExecutor(max_workers=10) as pool_:
        codes = Counter(pool_.map(cancel, range(10)))
    assert codes == {200: 1, 409: 9}, codes
    assert client.get(f"/products/{product['id']}/stock").json()["stock"] == 10


def test_a_flood_of_simultaneous_bad_logins_locks_the_account_and_never_succeeds(live_server, pool):
    """Fifty wrong-password attempts at once: every one is refused, the account locks, and nothing returns a 500."""
    user = make_user(pool, "staff")
    barrier = threading.Barrier(50)

    def attack(_):
        barrier.wait()
        return httpx.post(f"{live_server}/auth/login", json={"email": user["email"], "password": "wrong-password-123"},
                          timeout=30).status_code

    with ThreadPoolExecutor(max_workers=50) as pool_:
        codes = Counter(pool_.map(attack, range(50)))
    assert set(codes) <= {401, 429} and codes[401] >= 1
    assert scalar(pool, "SELECT locked_until IS NOT NULL FROM users WHERE id = %s", (user["id"],)) is True
