"""Who may do what. The table below is the contract; a test fails if a route is missing from it (needs TEST_DATABASE_URL)."""
import pytest

from app.security import ROLES
from tests.test_api import CUSTOMER

ALL = {"viewer", "staff", "admin"}
WRITERS = {"staff", "admin"}
ADMIN = {"admin"}

# (method, path template, json body, roles that are allowed in)
MATRIX = [
    ("GET", "/products", None, ALL),
    ("GET", "/products/{product_id}", None, ALL),
    ("GET", "/products/{product_id}/stock", None, ALL),
    ("GET", "/products/{product_id}/movements", None, ALL),
    ("GET", "/orders", None, ALL),
    ("GET", "/orders/{order_id}", None, ALL),
    ("GET", "/orders/{order_id}/history", None, ALL),
    ("GET", "/orders/{order_id}/invoice.pdf", None, ALL),
    ("POST", "/products", {}, WRITERS),
    ("PATCH", "/products/{product_id}/stock", {}, WRITERS),
    ("POST", "/orders", {}, WRITERS),
    ("PATCH", "/orders/{order_id}/status", {}, WRITERS),
    ("POST", "/orders/{order_id}/cancel", None, WRITERS),
    ("GET", "/reports", None, ADMIN),
    ("GET", "/reports/{report_id}", None, ADMIN),
    ("GET", "/ops/status", None, ADMIN),
    ("POST", "/ops/schedules/{name}/run-now", None, ADMIN),
    ("GET", "/notifications", None, ADMIN),
    ("GET", "/notifications/{notification_id}", None, ADMIN),
    ("POST", "/notifications/{notification_id}/retry", None, ADMIN),
    ("POST", "/notifications/{notification_id}/dismiss", None, ADMIN),
    ("GET", "/inventory/audit", None, ADMIN),
    ("GET", "/security/events", None, ADMIN),
    ("GET", "/users", None, ADMIN),
    ("POST", "/users", {}, ADMIN),
    ("PATCH", "/users/{user_id}", {}, ADMIN),
    ("POST", "/users/{user_id}/reset-password", {}, ADMIN),
]
ANY_SIGNED_IN = [("GET", "/auth/me", None), ("POST", "/auth/change-password", {}), ("POST", "/auth/logout", None)]
PUBLIC = {("GET", "/health"), ("POST", "/auth/login")}
IDS = dict(product_id=1, order_id=1, notification_id=1, report_id=1, user_id=1, name="daily_report")


def call(client, method, template, body):
    url = template.format(**IDS)
    return client.request(method, url, json=body) if body is not None else client.request(method, url)


def test_the_table_covers_every_route_in_the_api(web):
    declared = {(m.upper(), path) for path, methods in web.app.openapi()["paths"].items() for m in methods}
    covered = {(m, p) for m, p, _, _ in MATRIX} | {(m, p) for m, p, _ in ANY_SIGNED_IN} | PUBLIC
    assert declared == covered, f"missing from the table: {declared - covered}; stale entries: {covered - declared}"


@pytest.mark.parametrize("role", ROLES)
def test_each_role_can_do_exactly_what_it_should(web, role):
    client = web.client(role)
    for method, template, body, allowed in MATRIX:
        r = call(client, method, template, body)
        if role in allowed:
            assert r.status_code not in (401, 403), f"{role} should be allowed: {method} {template} -> {r.status_code}"
        else:
            assert r.status_code == 403, f"{role} must be refused: {method} {template} -> {r.status_code}"
            assert r.json()["error"]["code"] == "FORBIDDEN" and r.json()["error"]["details"][0]["permission"]


@pytest.mark.parametrize("role", ROLES)
def test_any_signed_in_user_can_reach_their_own_account_endpoints(web, role):
    client = web.client(role)
    for method, template, body in ANY_SIGNED_IN[:2]:
        assert call(client, method, template, body).status_code not in (401, 403)
    assert client.post("/auth/logout").status_code == 204


def test_a_viewer_cannot_change_anything_even_with_a_perfect_request(web):
    admin, viewer = web.client("admin"), web.client("viewer")
    p = admin.post("/products", json={"sku": "V-1", "name": "Viewed", "price": "5.00", "stock": 10}).json()
    assert viewer.post("/products", json={"sku": "V-2", "name": "Nope", "price": "5.00"}).status_code == 403
    r = viewer.post("/orders", json={"items": [{"product_id": p["id"], "quantity": 1}], "customer": CUSTOMER},
                    headers={"Idempotency-Key": "viewer-1"})
    assert r.status_code == 403
    assert viewer.patch(f"/products/{p['id']}/stock", json={"adjust_by": -5}).status_code == 403
    assert admin.get(f"/products/{p['id']}/stock").json()["stock"] == 10
    assert admin.get("/products").json()["total"] == 1 and admin.get("/orders").json()["total"] == 0


def test_staff_cannot_reach_admin_data_or_manage_users(web):
    staff = web.client("staff")
    for path in ("/users", "/reports", "/ops/status", "/notifications", "/security/events", "/inventory/audit"):
        assert staff.get(path).status_code == 403, path
    r = staff.post("/users", json={"email": "evil@example.com", "full_name": "Evil", "password": "Evil-Password-123",
                                   "role": "admin"})
    assert r.status_code == 403


def test_actions_are_attributed_to_the_signed_in_user(web):
    staff = web.client("staff")
    p = staff.post("/products", json={"sku": "ATT-1", "name": "Tracked", "price": "5.00", "stock": 10}).json()
    order = staff.post("/orders", json={"items": [{"product_id": p["id"], "quantity": 2}], "customer": CUSTOMER},
                       headers={"Idempotency-Key": "attr-1"}).json()
    staff.patch(f"/orders/{order['id']}/status", json={"status": "processing"})
    staff.patch(f"/products/{p['id']}/stock", json={"adjust_by": 1})
    admin = web.client("admin")
    history = admin.get(f"/orders/{order['id']}/history").json()["items"]
    assert [h["actor_id"] for h in history] == [staff.user["id"]] * 2
    movements = admin.get(f"/products/{p['id']}/movements").json()["items"]
    assert {m["actor_id"] for m in movements} == {staff.user["id"]}
