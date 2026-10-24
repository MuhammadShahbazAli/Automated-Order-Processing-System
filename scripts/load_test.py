"""End-to-end proof against a RUNNING Phase 5 server (authenticated).

Terminal 1:  uvicorn app.main:app
Terminal 2:  python scripts/load_test.py --email admin@example.com        (asks for the password)

1) N simultaneous buyers compete for a few units -> exactly that many must sell.
2) The SAME Idempotency-Key sent 30 times at once -> exactly one order.
Like a real client, it waits and retries on 503 (busy) and 429 (slow down), re-using the SAME key (safe).

Heads-up: the default write limit is 120/minute per user. For a 130-request burst either let the script wait
out the 429s, or start the server with RATE_LIMIT_WRITE=2000/minute RATE_LIMIT_IP=5000/minute.
The worker also writes one email file per order into outbox/ (delete them afterwards).
"""
import argparse
import getpass
import os
import random
import sys
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

parser = argparse.ArgumentParser()
parser.add_argument("--base-url", default="http://127.0.0.1:8000")
parser.add_argument("--email", default=os.environ.get("LOAD_TEST_EMAIL"))
parser.add_argument("--stock", type=int, default=10)
parser.add_argument("--buyers", type=int, default=100)
args = parser.parse_args()
if not args.email:
    sys.exit("Give an admin account: --email you@example.com (or set LOAD_TEST_EMAIL)")
password = os.environ.get("LOAD_TEST_PASSWORD") or getpass.getpass(f"Password for {args.email}: ")

client = httpx.Client(base_url=args.base_url, timeout=120, limits=httpx.Limits(max_connections=max(args.buyers, 30)))

# ---- preflight: make sure the server answering is really Phase 5 ----
try:
    spec = client.get("/openapi.json").json()
except httpx.ConnectError:
    sys.exit(f"Cannot reach {args.base_url}. Start the server first (uvicorn app.main:app) "
             "and wait for 'Application startup complete.'")
except ValueError:
    sys.exit("The server did not return an API description (docs disabled?). Set DOCS_ENABLED=1 for this test.")
if not spec["info"]["version"].startswith("5.") or "/auth/login" not in spec["paths"]:
    sys.exit("Wrong server on this port: this is not Phase 5 (v5.x). Stop old servers and restart.")

login = client.post("/auth/login", json={"email": args.email, "password": password})
if login.status_code != 200:
    sys.exit(f"Login failed ({login.status_code}): {login.json()['error']['message']}")
client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
if login.json()["user"]["role"] != "admin":
    sys.exit("Use an admin account (the script also reads /inventory/audit).")

retries = Counter()
CUSTOMER = {"name": "Load Test", "email": "load@example.com"}


def post_order(payload, key):
    """Retry on 503/429 with the SAME key (safe: idempotent). Returns the last response."""
    r = None
    for _ in range(15):
        r = client.post("/orders", json={**payload, "customer": CUSTOMER}, headers={"Idempotency-Key": key})
        if r.status_code not in (503, 429):
            return r
        retries[str(r.status_code)] += 1
        time.sleep(min(float(r.headers.get("Retry-After", 1)), 10) + random.random())
    return r


def order_id(r):
    try:
        return r.json().get("id")
    except ValueError:
        return None


# ---- test 1: simultaneous buyers ----
sku = f"LOAD-{uuid.uuid4().hex[:8]}"
product = client.post("/products", json={"sku": sku, "name": "Load test item", "price": "9.99",
                                         "stock": args.stock}).json()
pid = product["id"]
print(f"Product {pid}: stock {args.stock}, {args.buyers} simultaneous buyers (1 unit each)")


def buy(_):
    return post_order({"items": [{"product_id": pid, "quantity": 1}]}, str(uuid.uuid4())).status_code


with ThreadPoolExecutor(max_workers=args.buyers) as ex:
    codes = Counter(ex.map(buy, range(args.buyers)))

# ---- test 2: duplicate-key storm ----
dup_key = str(uuid.uuid4())
p2 = client.post("/products", json={"sku": sku + "-D", "name": "Dup item", "price": "5.00",
                                    "stock": 100}).json()


def dup(_):
    r = post_order({"items": [{"product_id": p2["id"], "quantity": 1}]}, dup_key)
    return r.status_code, order_id(r)


with ThreadPoolExecutor(max_workers=30) as ex:
    dup_results = list(ex.map(dup, range(30)))

final_stock = client.get(f"/products/{pid}/stock").json()["stock"]
dup_stock = client.get(f"/products/{p2['id']}/stock").json()["stock"]
audit = client.get("/inventory/audit").json()
dup_ids = {i for _, i in dup_results}
dup_codes = Counter(c for c, _ in dup_results)

print("Status codes:", dict(codes))
print("Retries:", dict(retries) or "none needed")
print("Final stock:", final_stock, f"(expected {max(args.stock - args.buyers, 0)})")
print("Duplicate storm: status codes", dict(dup_codes), "| order ids", dup_ids, "| stock left", dup_stock,
      "(expected 99)")
print("Audit:", audit)

checks = {
    "sold exactly the stock available": codes.get(201) == min(args.stock, args.buyers),
    "everyone else got 409 (sold out)": codes.get(409) == max(args.buyers - args.stock, 0),
    "no unexpected status codes": set(codes) <= {201, 409},
    "final stock is correct": final_stock == max(args.stock - args.buyers, 0),
    "duplicate storm created exactly one order": len(dup_ids) == 1 and None not in dup_ids,
    "duplicate storm deducted stock once": dup_stock == 99,
    "ledger audit consistent": audit.get("consistent") is True,
}
for name, passed in checks.items():
    print(("PASS  " if passed else "FAIL  ") + name)
print("RESULT:", "PASS - no overselling, no duplicates" if all(checks.values()) else "FAIL")
sys.exit(0 if all(checks.values()) else 1)
