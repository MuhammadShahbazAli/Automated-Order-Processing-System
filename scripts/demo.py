"""A guided demonstration of the whole backend, including everything that can go wrong.

    uvicorn app.main:app                                  (terminal 1)
    python scripts/demo.py --email admin@example.com      (terminal 2; asks for the password)

It creates its own temporary staff and viewer users and a demo product, so it is safe to run repeatedly.
Every line is a check: PASS means the system behaved exactly as promised.
"""
import argparse
import getpass
import os
import secrets
import sys
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

parser = argparse.ArgumentParser()
parser.add_argument("--base-url", default="http://127.0.0.1:8000")
parser.add_argument("--email", default=os.environ.get("DEMO_ADMIN_EMAIL"))
parser.add_argument("--skip-rate-limit", action="store_true", help="skip the final step that briefly throttles logins")
args = parser.parse_args()
if not args.email:
    sys.exit("Give an admin account: --email you@example.com (or set DEMO_ADMIN_EMAIL)")
admin_password = os.environ.get("DEMO_ADMIN_PASSWORD") or getpass.getpass(f"Password for {args.email}: ")

results = []


def step(title):
    print(f"\n== {title}")


def check(label, condition, detail=""):
    results.append(bool(condition))
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  ({detail})" if detail and not condition else ""))
    return condition


def api(token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(base_url=args.base_url, headers=headers, timeout=60,
                        limits=httpx.Limits(max_connections=60))


def sign_in(email, password):
    r = httpx.post(f"{args.base_url}/auth/login", json={"email": email, "password": password}, timeout=30)
    return r, (r.json().get("access_token") if r.status_code == 200 else None)


def code(r):
    try:
        return r.json()["error"]["code"]
    except Exception:
        return None


try:
    httpx.get(f"{args.base_url}/health", timeout=5).raise_for_status()
except Exception as exc:
    sys.exit(f"Cannot reach the server at {args.base_url}: {exc}")

r, admin_token = sign_in(args.email, admin_password)
if r.status_code != 200:
    sys.exit(f"Admin login failed ({r.status_code}). Check the email and password.")
admin = api(admin_token)

run = secrets.token_hex(3)
staff_email, viewer_email = f"demo-staff-{run}@example.com", f"demo-viewer-{run}@example.com"
user_password = secrets.token_urlsafe(12) + "-Aa1"
for email, role in ((staff_email, "staff"), (viewer_email, "viewer")):
    admin.post("/users", json={"email": email, "full_name": f"Demo {role}", "password": user_password, "role": role})
_, staff_token = sign_in(staff_email, user_password)
_, viewer_token = sign_in(viewer_email, user_password)
staff, viewer, anon = api(staff_token), api(viewer_token), api()
CUSTOMER = {"name": "Demo Customer", "email": f"customer-{run}@example.com", "address": "12 Mall Road, Lahore"}


def place(client, product_id, qty, key=None, **extra):
    return client.post("/orders", headers={"Idempotency-Key": key or str(uuid.uuid4())},
                       json={"customer": CUSTOMER, "items": [{"product_id": product_id, "quantity": qty}], **extra})


step("1. Security: who is allowed in")
check("No token -> 401", anon.get("/products").status_code == 401)
check("Garbage token -> 401", api("inv_garbage").get("/products").status_code == 401)
check("Wrong password -> 401 with a generic message",
      sign_in(staff_email, "wrong-password-123")[0].status_code == 401)
check("Viewer can read products", viewer.get("/products").status_code == 200)
r = viewer.post("/products", json={"sku": f"NOPE-{run}", "name": "Not allowed", "price": "1.00"})
check("Viewer cannot create products -> 403", r.status_code == 403 and code(r) == "FORBIDDEN")
check("Staff cannot manage users -> 403", staff.get("/users").status_code == 403)

step("2. A product and an order")
product = staff.post("/products", json={"sku": f"DEMO-{run}", "name": "Demo widget", "price": "250.00",
                                        "stock": 5, "low_stock_threshold": 2}).json()
pid = product["id"]
r = place(staff, pid, 2, key=f"order-{run}")
order = r.json()
check("Order accepted (201) and the server calculated the total", r.status_code == 201 and order["total"] == "500.00",
      r.text[:200])
check("Stock went 5 -> 3", staff.get(f"/products/{pid}/stock").json()["stock"] == 3)

step("3. Bad input is refused, and changes nothing")
r = place(staff, pid, 0)
check("Quantity 0 -> 422 with details", r.status_code == 422 and code(r) == "VALIDATION_ERROR" and r.json()["error"]["details"])
r = place(staff, pid, 1, total="0.01")
check("Client-supplied total -> 422 (totals are server-side only)", r.status_code == 422)
r = place(staff, 99999999, 1)
check("Unknown product -> 404", r.status_code == 404 and code(r) == "PRODUCT_NOT_FOUND")
r = staff.post("/orders", json={"customer": CUSTOMER, "items": [{"product_id": pid, "quantity": 1}]})
check("Missing Idempotency-Key -> 400", r.status_code == 400 and code(r) == "IDEMPOTENCY_KEY_REQUIRED")
check("Stock is still 3", staff.get(f"/products/{pid}/stock").json()["stock"] == 3)

step("4. Not enough stock")
r = place(staff, pid, 4)
detail = r.json()["error"]["details"][0] if r.status_code == 409 else {}
check("Asking for 4 when 3 are left -> 409 with the numbers",
      r.status_code == 409 and (detail.get("requested"), detail.get("available")) == (4, 3), r.text[:200])

step("5. Duplicate submission (a double-click or a network retry)")
again = place(staff, pid, 2, key=f"order-{run}")
check("Same key again -> 200 'replay' returning the SAME order",
      again.status_code == 200 and again.headers.get("idempotent-replay") == "true" and again.json()["id"] == order["id"])
check("Stock was deducted only once (still 3)", staff.get(f"/products/{pid}/stock").json()["stock"] == 3)
r = place(staff, pid, 3, key=f"order-{run}")
check("Same key with a DIFFERENT request -> 422", r.status_code == 422 and code(r) == "IDEMPOTENCY_KEY_REUSED")

step("6. Many buyers at once")
hot = staff.post("/products", json={"sku": f"HOT-{run}", "name": "Last three", "price": "10.00", "stock": 3}).json()


def buy(_):
    for _try in range(10):
        resp = place(staff, hot["id"], 1, key=str(uuid.uuid4()))
        if resp.status_code not in (429, 503):
            return resp.status_code
    return resp.status_code


with ThreadPoolExecutor(max_workers=12) as ex:
    codes = Counter(ex.map(buy, range(12)))
check("12 simultaneous buyers, 3 units: exactly 3 succeed, 9 told 'sold out'", codes == {201: 3, 409: 9}, str(dict(codes)))
check("Stock is exactly 0 (never negative)", staff.get(f"/products/{hot['id']}/stock").json()["stock"] == 0)

step("7. Order lifecycle and cancellation")
check("confirmed -> processing", staff.patch(f"/orders/{order['id']}/status", json={"status": "processing"}).status_code == 200)
r = staff.patch(f"/orders/{order['id']}/status", json={"status": "completed"})
check("Skipping a step (processing -> completed) -> 409", r.status_code == 409 and code(r) == "INVALID_STATUS_TRANSITION")
check("processing -> shipped", staff.patch(f"/orders/{order['id']}/status", json={"status": "shipped"}).status_code == 200)
check("A shipped order cannot be cancelled -> 409", staff.post(f"/orders/{order['id']}/cancel").status_code == 409)
check("shipped -> completed", staff.patch(f"/orders/{order['id']}/status", json={"status": "completed"}).status_code == 200)
second = place(staff, pid, 3).json()
check("Stock is 0 after a second order", staff.get(f"/products/{pid}/stock").json()["stock"] == 0)
check("Cancelling returns the stock (0 -> 3)", staff.post(f"/orders/{second['id']}/cancel", json={"reason": "demo"}).status_code == 200
      and staff.get(f"/products/{pid}/stock").json()["stock"] == 3)
check("Cancelling twice -> 409 (stock not returned twice)",
      staff.post(f"/orders/{second['id']}/cancel").status_code == 409 and staff.get(f"/products/{pid}/stock").json()["stock"] == 3)
history = admin.get(f"/orders/{order['id']}/history").json()["items"]
check("History records every step and who did it", [h["to_status"] for h in history] ==
      ["confirmed", "processing", "shipped", "completed"] and all(h["actor_id"] for h in history))

step("8. Invoice")
pdf = viewer.get(f"/orders/{order['id']}/invoice.pdf")
open("demo-invoice.pdf", "wb").write(pdf.content)
check("PDF invoice downloaded (saved as demo-invoice.pdf)", pdf.status_code == 200 and pdf.content.startswith(b"%PDF"))

step("9. Operations and audit (admin)")
status = admin.get("/ops/status").json()
check("Operations status reports worker, queue and schedules", {"worker", "notifications", "schedules"} <= set(status))
check("Inventory ledger is consistent", admin.get("/inventory/audit").json()["consistent"] is True)
events = {e["event"] for e in admin.get("/security/events").json()["items"]}
check("Security log shows logins and failures", {"login_ok", "login_failed"} <= events)
check("Staff cannot read the security log -> 403", staff.get("/security/events").status_code == 403)

if not args.skip_rate_limit:
    step("10. Rate limiting (this briefly blocks LOGINS from this computer, about a minute)")
    statuses = [httpx.post(f"{args.base_url}/auth/login", json={"email": f"nobody-{i}@example.com", "password": "x" * 12},
                           timeout=30).status_code for i in range(15)]
    check("A burst of bad logins is throttled with 429", 429 in statuses, str(Counter(statuses)))

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
