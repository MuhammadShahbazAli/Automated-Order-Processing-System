"""HTTP-level tests. Requires: pip install -r requirements-dev.txt, then run: pytest"""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(str(tmp_path / "test.db")))


def make_product(client, sku="SKU-1", price="10.00", stock=10):
    r = client.post("/products", json={"sku": sku, "name": f"Item {sku}", "price": price, "stock": stock})
    assert r.status_code == 201, r.text
    return r.json()


def test_create_and_list_products(client):
    p = make_product(client, price="19.99", stock=5)
    assert p["price"] == "19.99" and p["stock"] == 5
    r = client.get("/products")
    assert r.status_code == 200 and r.json()["total"] == 1


def test_create_product_validation_errors(client):
    r = client.post("/products", json={"sku": "", "name": "x", "price": "-1", "stock": -5, "color": "red"})
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    fields = {d["field"] for d in body["details"]}
    assert {"sku", "price", "stock", "color"} <= fields


def test_duplicate_sku_returns_409(client):
    make_product(client, "DUP")
    r = client.post("/products", json={"sku": "dup", "name": "again", "price": "1.00"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "DUPLICATE_SKU"


def test_stock_view_and_update(client):
    p = make_product(client, stock=10)
    assert client.get(f"/products/{p['id']}/stock").json()["stock"] == 10
    r = client.patch(f"/products/{p['id']}/stock", json={"adjust_by": -3})
    assert r.status_code == 200 and r.json()["stock"] == 7
    r = client.patch(f"/products/{p['id']}/stock", json={"set_to": 40})
    assert r.json() == {"product_id": p["id"], "stock": 40, "previous_stock": 7}


def test_stock_update_rules(client):
    p = make_product(client, stock=2)
    url = f"/products/{p['id']}/stock"
    assert client.patch(url, json={}).status_code == 422                          # neither field
    assert client.patch(url, json={"set_to": 1, "adjust_by": 1}).status_code == 422  # both fields
    r = client.patch(url, json={"adjust_by": -5})
    assert r.status_code == 409 and r.json()["error"]["code"] == "INSUFFICIENT_STOCK"
    assert client.patch("/products/999/stock", json={"set_to": 1}).status_code == 404


def test_submit_order_success(client):
    a = make_product(client, "A", "19.99", 10)
    b = make_product(client, "B", "0.10", 10)
    r = client.post("/orders", json={"items": [{"product_id": a["id"], "quantity": 3},
                                               {"product_id": b["id"], "quantity": 3}]})
    assert r.status_code == 201
    order = r.json()
    assert order["total"] == "60.27" and order["status"] == "confirmed"
    assert client.get(f"/products/{a['id']}/stock").json()["stock"] == 7
    assert client.get(f"/orders/{order['id']}").json()["total"] == "60.27"


@pytest.mark.parametrize("item", [
    {"product_id": 1, "quantity": 0},
    {"product_id": 1, "quantity": -2},
    {"product_id": 1, "quantity": 1.5},
    {"product_id": 1, "quantity": "2"},
    {"product_id": 1, "quantity": True},
    {"product_id": 0, "quantity": 1},
    {"product_id": "abc", "quantity": 1},
    {"product_id": 1},
])
def test_order_rejects_bad_items(client, item):
    make_product(client)
    r = client.post("/orders", json={"items": [item]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_order_rejects_empty_and_client_supplied_totals(client):
    make_product(client)
    assert client.post("/orders", json={"items": []}).status_code == 422
    r = client.post("/orders", json={"items": [{"product_id": 1, "quantity": 1, "price": "0.01"}], "total": "0.01"})
    assert r.status_code == 422  # unknown fields are refused: totals are server-side only


def test_order_unknown_product_and_insufficient_stock(client):
    a = make_product(client, "A", stock=5)
    r = client.post("/orders", json={"items": [{"product_id": a["id"], "quantity": 1},
                                               {"product_id": 999, "quantity": 1}]})
    assert r.status_code == 404 and r.json()["error"]["code"] == "PRODUCT_NOT_FOUND"
    r = client.post("/orders", json={"items": [{"product_id": a["id"], "quantity": 6}]})
    assert r.status_code == 409
    assert r.json()["error"]["details"][0]["available"] == 5
    assert client.get(f"/products/{a['id']}/stock").json()["stock"] == 5  # nothing deducted


def test_unknown_route_and_bad_path_use_same_error_shape(client):
    assert client.get("/nope").json()["error"]["code"] == "NOT_FOUND"
    assert client.get("/products/abc").status_code == 422
    assert client.get("/products/999").status_code == 404
