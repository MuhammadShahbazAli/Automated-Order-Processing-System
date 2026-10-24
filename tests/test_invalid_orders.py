"""Invalid input must be refused cleanly and must never change any data (needs TEST_DATABASE_URL)."""
import json

import pytest

from tests.conftest import scalar
from tests.test_api import CUSTOMER, make_product

TABLES = ("orders", "order_items", "idempotency_keys", "inventory_movements", "notifications")


def snapshot(pool, product_id):
    counts = {t: scalar(pool, f"SELECT count(*) FROM {t}") for t in TABLES}
    counts["stock"] = scalar(pool, "SELECT stock FROM products WHERE id = %s", (product_id,))
    return counts


def item(pid, quantity=1):
    return {"product_id": pid, "quantity": quantity}


def order_body(pid, **changes):
    body = {"customer": dict(CUSTOMER), "items": [item(pid)]}
    body.update(changes)
    return body


def with_item(pid, **fields):
    return order_body(pid, items=[{**item(pid), **fields}])


def with_customer(pid, **fields):
    return order_body(pid, customer={**CUSTOMER, **fields})


BODIES = {
    "empty object": lambda p: {},
    "no customer": lambda p: {"items": [item(p)]},
    "no items": lambda p: {"customer": CUSTOMER},
    "empty items": lambda p: order_body(p, items=[]),
    "items is a string": lambda p: order_body(p, items="abc"),
    "item is a number": lambda p: order_body(p, items=[5]),
    "101 lines": lambda p: order_body(p, items=[item(p + i) for i in range(101)]),
    "quantity zero": lambda p: with_item(p, quantity=0),
    "quantity negative": lambda p: with_item(p, quantity=-3),
    "quantity fractional": lambda p: with_item(p, quantity=1.5),
    "quantity as text": lambda p: with_item(p, quantity="2"),
    "quantity boolean": lambda p: with_item(p, quantity=True),
    "quantity null": lambda p: with_item(p, quantity=None),
    "quantity above limit": lambda p: with_item(p, quantity=10_001),
    "quantity astronomical": lambda p: with_item(p, quantity=10 ** 12),
    "quantity missing": lambda p: order_body(p, items=[{"product_id": p}]),
    "product id missing": lambda p: order_body(p, items=[{"quantity": 1}]),
    "product id zero": lambda p: with_item(p, product_id=0),
    "product id negative": lambda p: with_item(p, product_id=-1),
    "product id text": lambda p: with_item(p, product_id="abc"),
    "product id fractional": lambda p: with_item(p, product_id=1.5),
    "product id beyond bigint": lambda p: with_item(p, product_id=2 ** 63),
    "product id null": lambda p: with_item(p, product_id=None),
    "client-supplied price": lambda p: with_item(p, price="0.01"),
    "client-supplied total": lambda p: order_body(p, total="0.01"),
    "unknown customer field": lambda p: with_customer(p, discount="100%"),
    "customer is text": lambda p: order_body(p, customer="bob"),
    "customer email missing": lambda p: order_body(p, customer={"name": "Bob"}),
    "customer email junk": lambda p: with_customer(p, email="oylad@8.io0_b!}"),
    "customer email list": lambda p: with_customer(p, email="a@example.com,b@example.com"),
    "customer email spaces": lambda p: with_customer(p, email="a b@example.com"),
    "customer name blank": lambda p: with_customer(p, name="   "),
    "customer name too long": lambda p: with_customer(p, name="x" * 201),
    "customer address too long": lambda p: with_customer(p, address="x" * 501),
    "customer phone too long": lambda p: with_customer(p, phone="1" * 41),
}


@pytest.fixture()
def setup(web, pool):
    staff = web.client("staff")
    product = make_product(staff, "INV-1", "10.00", 10)
    return staff, product


@pytest.mark.parametrize("label", list(BODIES))
def test_invalid_orders_are_refused_and_change_nothing(setup, pool, label):
    staff, product = setup
    before = snapshot(pool, product["id"])
    r = staff.post("/orders", json=BODIES[label](product["id"]), headers={"Idempotency-Key": f"bad-{label}"})
    assert r.status_code == 422, f"{label}: {r.status_code} {r.text}"
    error = r.json()["error"]
    assert error["code"] == "VALIDATION_ERROR" and error["details"] and error["request_id"]
    assert snapshot(pool, product["id"]) == before


@pytest.mark.parametrize("raw", ["{bad json", "", "[]", "null", '"just text"', "{'single': 'quotes'}",
                                 '{"customer": {"name": "x"}, "items": [}', "\xff\xfe\x00"])
def test_malformed_json_is_a_clean_client_error(setup, pool, raw):
    staff, product = setup
    before = snapshot(pool, product["id"])
    r = staff.post("/orders", content=raw.encode("latin-1"),
                   headers={"Content-Type": "application/json", "Idempotency-Key": "k"})
    assert r.status_code in (400, 422) and r.json()["error"]["code"] in ("VALIDATION_ERROR", "BAD_REQUEST")
    assert snapshot(pool, product["id"]) == before


def test_wrong_content_type_is_refused(setup):
    staff, product = setup
    r = staff.post("/orders", content=json.dumps(order_body(product["id"])),
                   headers={"Content-Type": "text/plain", "Idempotency-Key": "k"})
    assert r.status_code in (400, 415, 422)


def test_oversized_body_is_refused_before_it_is_parsed(setup, pool):
    staff, product = setup
    before = snapshot(pool, product["id"])
    r = staff.post("/orders", json=order_body(product["id"], customer={**CUSTOMER, "address": "x" * 1_200_000}),
                   headers={"Idempotency-Key": "huge"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert snapshot(pool, product["id"]) == before


def test_idempotency_key_rules(setup):
    staff, product = setup
    body = order_body(product["id"])
    assert staff.post("/orders", json=body).status_code == 400                                   # missing
    assert staff.post("/orders", json=body, headers={"Idempotency-Key": "   "}).status_code == 400   # blank
    assert staff.post("/orders", json=body, headers={"Idempotency-Key": "k" * 256}).status_code == 400
    assert staff.post("/orders", json=body, headers={"Idempotency-Key": "k" * 255}).status_code == 201


def test_unknown_products_are_all_reported_and_nothing_is_taken(setup, pool):
    staff, product = setup
    before = snapshot(pool, product["id"])
    r = staff.post("/orders", headers={"Idempotency-Key": "ghosts"}, json=order_body(
        product["id"], items=[item(product["id"]), item(9001), item(9002)]))
    assert r.status_code == 404 and r.json()["error"]["code"] == "PRODUCT_NOT_FOUND"
    assert sorted(d["product_id"] for d in r.json()["error"]["details"]) == [9001, 9002]
    assert snapshot(pool, product["id"]) == before


def test_largest_valid_product_id_is_a_404_not_a_crash(setup):
    staff, _ = setup
    r = staff.post("/orders", headers={"Idempotency-Key": "big"},
                   json=order_body(1, items=[item(2 ** 63 - 1)]))
    assert r.status_code == 404


@pytest.mark.parametrize("path,expected", [
    ("/products/99999999999999999999", 404), ("/products/0", 422), ("/products/-1", 422), ("/products/abc", 422),
    ("/orders/0", 422), ("/orders/abc", 422), ("/orders/99999999999999999999", 404),
    ("/orders/1.5", 422), ("/orders/1/invoice.pdf", 404)])
def test_bad_ids_in_the_url(setup, path, expected):
    staff, _ = setup
    assert staff.get(path).status_code == expected


@pytest.mark.parametrize("body", [{"set_to": -1}, {"adjust_by": 10 ** 9}, {"set_to": 10 ** 9}, {"set_to": 1.5},
                                  {"set_to": "5"}, {"set_to": True}, {"set_to": 1, "adjust_by": 1}, {}])
def test_invalid_stock_changes(setup, pool, body):
    staff, product = setup
    before = snapshot(pool, product["id"])
    assert staff.patch(f"/products/{product['id']}/stock", json=body).status_code == 422
    assert snapshot(pool, product["id"]) == before


@pytest.mark.parametrize("body", [
    {"sku": "", "name": "x", "price": "1.00"}, {"sku": "S", "name": "x", "price": "0"},
    {"sku": "S", "name": "x", "price": "-5.00"}, {"sku": "S", "name": "x", "price": "1.999"},
    {"sku": "S", "name": "x", "price": "NaN"}, {"sku": "S", "name": "x", "price": "Infinity"},
    {"sku": "S", "name": "x", "price": "12345678901"}, {"sku": "S", "name": "x", "price": "abc"},
    {"sku": "S" * 65, "name": "x", "price": "1.00"}, {"sku": "S", "name": "x" * 201, "price": "1.00"},
    {"sku": "S", "name": "x", "price": "1.00", "stock": -1}, {"sku": "S", "name": "x", "price": "1.00", "stock": 1.5},
    {"sku": "S", "name": "x", "price": "1.00", "id": 5}])
def test_invalid_products(setup, body):
    staff, _ = setup
    assert staff.post("/products", json=body).status_code == 422


def test_hostile_text_is_stored_as_plain_text_and_breaks_nothing(setup, pool):
    staff, product = setup
    sku, name = "Robert'); DROP TABLE orders;--", "'; DROP TABLE products; --"
    created = staff.post("/products", json={"sku": sku, "name": name, "price": "5.00", "stock": 5})
    assert created.status_code == 201 and created.json()["sku"] == sku and created.json()["name"] == name
    customer = {"name": "<script>alert(1)</script>", "email": "x@example.com",
                "address": "Line one\nLine two (with) \\ odd 'quotes' \"here\""}
    r = staff.post("/orders", headers={"Idempotency-Key": "hostile"},
                   json={"customer": customer, "items": [item(created.json()["id"])]})
    assert r.status_code == 201 and r.json()["customer"]["name"] == customer["name"]
    invoice = staff.get(f"/orders/{r.json()['id']}/invoice.pdf")
    assert invoice.status_code == 200 and invoice.content.startswith(b"%PDF")
    assert scalar(pool, "SELECT count(*) FROM products") == 2                         # nothing was dropped
    assert scalar(pool, "SELECT count(*) FROM orders") == 1


def test_non_latin_names_work_end_to_end(setup):
    staff, _ = setup
    p = staff.post("/products", json={"sku": "UR-1", "name": "قلم (Pen) 笔", "price": "5.00", "stock": 5}).json()
    r = staff.post("/orders", headers={"Idempotency-Key": "urdu"}, json={
        "customer": {"name": "محمد علی", "email": "ali@example.com"}, "items": [item(p["id"])]})
    assert r.status_code == 201 and r.json()["customer"]["name"] == "محمد علی"
    assert staff.get(f"/orders/{r.json()['id']}/invoice.pdf").status_code == 200
