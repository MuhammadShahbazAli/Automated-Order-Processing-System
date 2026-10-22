"""HTTP-level tests (requires TEST_DATABASE_URL, see conftest.py)."""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(db_url, pool):   # `pool` fixture wipes the tables first
    with TestClient(create_app(db_url, auto_migrate=False)) as c:
        yield c


def make_product(client, sku="SKU-1", price="10.00", stock=10, **extra):
    r = client.post("/products", json={"sku": sku, "name": f"Item {sku}", "price": price, "stock": stock, **extra})
    assert r.status_code == 201, r.text
    return r.json()


CUSTOMER = {"name": "Ayesha Khan", "email": "ayesha@example.com"}


def place(client, items, key="key-1", customer=CUSTOMER):
    return client.post("/orders", json={"items": items, "customer": customer}, headers={"Idempotency-Key": key})


def test_create_and_list_products(client):
    p = make_product(client, price="19.99", stock=5)
    assert p["price"] == "19.99" and p["stock"] == 5 and p["low_stock_threshold"] == 5
    r = client.get("/products")
    assert r.status_code == 200 and r.json()["total"] == 1


def test_create_product_validation_and_duplicates(client):
    r = client.post("/products", json={"sku": "", "name": "x", "price": "-1", "stock": -5, "color": "red"})
    assert r.status_code == 422
    assert {"sku", "price", "stock", "color"} <= {d["field"] for d in r.json()["error"]["details"]}
    make_product(client, "DUP")
    r = client.post("/products", json={"sku": "dup", "name": "again", "price": "1.00"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "DUPLICATE_SKU"


def test_stock_view_update_and_rules(client):
    p = make_product(client, stock=10)
    url = f"/products/{p['id']}/stock"
    assert client.get(url).json()["stock"] == 10
    assert client.patch(url, json={"adjust_by": -3}).json()["stock"] == 7
    assert client.patch(url, json={"set_to": 40}).json() == {"product_id": p["id"], "stock": 40, "previous_stock": 7}
    assert client.patch(url, json={}).status_code == 422
    assert client.patch(url, json={"set_to": 1, "adjust_by": 1}).status_code == 422
    r = client.patch(url, json={"adjust_by": -500})
    assert r.status_code == 409 and r.json()["error"]["code"] == "INSUFFICIENT_STOCK"
    assert client.patch("/products/999/stock", json={"set_to": 1}).status_code == 404


def test_order_success_and_idempotent_replay(client):
    a = make_product(client, "A", "19.99", 10)
    b = make_product(client, "B", "0.10", 10)
    items = [{"product_id": a["id"], "quantity": 3}, {"product_id": b["id"], "quantity": 3}]
    r = place(client, items, "order-1")
    assert r.status_code == 201 and r.json()["total"] == "60.27"
    assert "idempotent-replay" not in r.headers

    again = place(client, items, "order-1")                       # client retry
    assert again.status_code == 200 and again.headers["Idempotent-Replay"] == "true"
    assert again.json()["id"] == r.json()["id"]
    assert client.get(f"/products/{a['id']}/stock").json()["stock"] == 7   # deducted once

    other = place(client, [{"product_id": a["id"], "quantity": 1}], "order-1")
    assert other.status_code == 422 and other.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert client.get(f"/orders/{r.json()['id']}").json()["total"] == "60.27"


def test_idempotency_key_is_required(client):
    p = make_product(client)
    r = client.post("/orders", json={"items": [{"product_id": p["id"], "quantity": 1}], "customer": CUSTOMER})
    assert r.status_code == 400 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


@pytest.mark.parametrize("item", [
    {"product_id": 1, "quantity": 0}, {"product_id": 1, "quantity": -2}, {"product_id": 1, "quantity": 1.5},
    {"product_id": 1, "quantity": "2"}, {"product_id": 1, "quantity": True}, {"product_id": 0, "quantity": 1},
    {"product_id": "abc", "quantity": 1}, {"product_id": 1},
])
def test_order_rejects_bad_items(client, item):
    make_product(client)
    r = place(client, [item])
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_order_rejects_empty_and_client_supplied_totals(client):
    make_product(client)
    assert place(client, []).status_code == 422
    r = client.post("/orders", headers={"Idempotency-Key": "k"},
                    json={"items": [{"product_id": 1, "quantity": 1, "price": "0.01"}], "total": "0.01",
                          "customer": CUSTOMER})
    assert r.status_code == 422


def test_order_unknown_product_and_insufficient_stock(client):
    a = make_product(client, "A", stock=5)
    r = place(client, [{"product_id": a["id"], "quantity": 1}, {"product_id": 999, "quantity": 1}], "k1")
    assert r.status_code == 404 and r.json()["error"]["code"] == "PRODUCT_NOT_FOUND"
    r = place(client, [{"product_id": a["id"], "quantity": 6}], "k2")
    assert r.status_code == 409 and r.json()["error"]["details"][0]["available"] == 5
    assert client.get(f"/products/{a['id']}/stock").json()["stock"] == 5


def test_movements_notifications_and_audit_endpoints(client):
    p = make_product(client, stock=6, low_stock_threshold=5)
    assert place(client, [{"product_id": p["id"], "quantity": 2}], "k").status_code == 201
    moves = client.get(f"/products/{p['id']}/movements").json()
    assert [m["change"] for m in moves["items"]] == [6, -2]
    types = [n["type"] for n in client.get("/notifications?status=pending").json()["items"]]
    assert sorted(types) == ["low_stock", "order_confirmed"]
    assert client.get("/inventory/audit").json() == {"consistent": True, "discrepancies": []}


def test_unknown_route_and_bad_path_use_same_error_shape(client):
    assert client.get("/nope").json()["error"]["code"] == "NOT_FOUND"
    assert client.get("/products/abc").status_code == 422
    assert client.get("/products/999").status_code == 404


# ---------------- Phase 3: customer, lifecycle, invoice ----------------
def test_orders_require_a_valid_customer(client):
    p = make_product(client)
    item = [{"product_id": p["id"], "quantity": 1}]
    hdr = {"Idempotency-Key": "k"}
    assert client.post("/orders", json={"items": item}, headers=hdr).status_code == 422          # missing
    for bad in ({"name": "A", "email": "not-an-email"}, {"name": "", "email": "a@example.com"},
                {"name": "A", "email": "a@example.com", "extra": 1}):
        assert place(client, item, "k", customer=bad).status_code == 422


def test_full_lifecycle_over_http(client):
    p = make_product(client, stock=10)
    order = place(client, [{"product_id": p["id"], "quantity": 2}], "life-1").json()
    oid = order["id"]
    assert order["status"] == "confirmed" and order["customer"]["name"] == "Ayesha Khan"
    assert order["invoice_number"] == f"INV-{oid:06d}"

    for step in ("processing", "shipped", "completed"):
        r = client.patch(f"/orders/{oid}/status", json={"status": step, "note": f"to {step}"})
        assert r.status_code == 200 and r.json()["status"] == step

    history = client.get(f"/orders/{oid}/history").json()["items"]
    assert [h["to_status"] for h in history] == ["confirmed", "processing", "shipped", "completed"]
    assert client.get("/orders?status=completed").json()["total"] == 1
    assert client.get("/orders?status=confirmed").json()["total"] == 0
    assert client.get("/orders").json()["items"][0]["invoice_number"] == f"INV-{oid:06d}"
    assert client.get(f"/products/{p['id']}/stock").json()["stock"] == 8


def test_invalid_transitions_return_409_with_allowed_steps(client):
    p = make_product(client)
    oid = place(client, [{"product_id": p["id"], "quantity": 1}], "t1").json()["id"]
    r = client.patch(f"/orders/{oid}/status", json={"status": "completed"})
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "INVALID_STATUS_TRANSITION"
    assert err["details"][0]["allowed"] == ["cancelled", "processing"]
    assert client.patch(f"/orders/{oid}/status", json={"status": "confirmed"}).status_code == 422
    assert client.patch(f"/orders/{oid}/status", json={"status": "bogus"}).status_code == 422
    assert client.patch("/orders/999/status", json={"status": "processing"}).status_code == 404


def test_cancel_endpoint_restores_stock(client):
    p = make_product(client, stock=10)
    oid = place(client, [{"product_id": p["id"], "quantity": 4}], "c1").json()["id"]
    assert client.get(f"/products/{p['id']}/stock").json()["stock"] == 6
    r = client.post(f"/orders/{oid}/cancel", json={"reason": "Changed mind"})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert r.json()["cancel_reason"] == "Changed mind"
    assert client.get(f"/products/{p['id']}/stock").json()["stock"] == 10
    again = client.post(f"/orders/{oid}/cancel")                      # no body is fine, but too late
    assert again.status_code == 409 and again.json()["error"]["code"] == "INVALID_STATUS_TRANSITION"
    assert client.get("/inventory/audit").json()["consistent"] is True


def test_cancel_without_body_works(client):
    p = make_product(client, stock=3)
    oid = place(client, [{"product_id": p["id"], "quantity": 1}], "c2").json()["id"]
    assert client.post(f"/orders/{oid}/cancel").json()["status"] == "cancelled"


def test_invoice_pdf_is_downloadable(client):
    p = make_product(client, "INV-ITEM", "19.99", 10)
    oid = place(client, [{"product_id": p["id"], "quantity": 3}], "i1").json()["id"]
    r = client.get(f"/orders/{oid}/invoice.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert f"INV-{oid:06d}.pdf" in r.headers["content-disposition"]
    assert r.content.startswith(b"%PDF") and b"Ayesha Khan" in r.content and b"59.97" in r.content
    client.post(f"/orders/{oid}/cancel")
    assert b"CANCELLED" in client.get(f"/orders/{oid}/invoice.pdf").content
    missing = client.get("/orders/999/invoice.pdf")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "ORDER_NOT_FOUND"
