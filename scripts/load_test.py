"""End-to-end proof against a RUNNING Phase 2 server.

Terminal 1:  uvicorn app.main:app
Terminal 2:  python scripts/load_test.py

1) N simultaneous buyers compete for a few units -> exactly that many must sell.
2) The SAME Idempotency-Key sent 30 times at once -> exactly one order.
Like a real client, it retries on 503 (server busy) with the SAME key, which is safe because
orders are idempotent.
"""
import argparse
import random
import sys
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

parser = argparse.ArgumentParser()
parser.add_argument("--base-url", default="http://127.0.0.1:8000")
parser.add_argument("--stock", type=int, default=10)
parser.add_argument("--buyers", type=int, default=100)
args = parser.parse_args()

client = httpx.Client(base_url=args.base_url, timeout=60,
                      limits=httpx.Limits(max_connections=max(args.buyers, 30)))

# ---- preflight: make sure the server answering is really Phase 2 ----
try:
    spec = client.get("/openapi.json").json()
except httpx.ConnectError:
    sys.exit(f"Cannot reach {args.base_url}. Start the server first (uvicorn app.main:app) "
             "and wait for 'Application startup complete.'")
if spec["info"]["version"] != "2.0.0" or "/inventory/audit" not in spec["paths"]:
    sys.exit("Wrong server on this port: this is not Phase 2 (v2.0.0). Stop old servers and restart.")

retries = Counter()


def post_order(payload, key):
    """Retry on 503 with the SAME key (safe: idempotent). Returns the last response."""
    r = None
    for _ in range(10):
        r = client.post("/orders", json=payload, headers={"Idempotency-Key": key})
        if r.status_code != 503:
            return r
        retries["503"] += 1
        time.sleep(float(r.headers.get("Retry-After", 1)) + random.random())
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
print("503 responses retried:", retries["503"])
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