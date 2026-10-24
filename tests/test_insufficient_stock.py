"""Stock shortages: exact boundaries, all-or-nothing, clear details (needs TEST_DATABASE_URL)."""
from tests.conftest import scalar
from tests.test_api import make_product, place


def stock(client, pid):
    return client.get(f"/products/{pid}/stock").json()["stock"]


def test_buying_exactly_what_is_left_works_and_one_more_does_not(web):
    staff = web.client("staff")
    p = make_product(staff, "EDGE", "10.00", 5)
    assert place(staff, [{"product_id": p["id"], "quantity": 5}], "all").status_code == 201
    assert stock(staff, p["id"]) == 0
    r = place(staff, [{"product_id": p["id"], "quantity": 1}], "one-more")
    assert r.status_code == 409
    detail = r.json()["error"]["details"][0]
    assert r.json()["error"]["code"] == "INSUFFICIENT_STOCK"
    assert (detail["product_id"], detail["requested"], detail["available"]) == (p["id"], 1, 0)


def test_one_over_the_available_quantity_is_refused_with_the_numbers(web):
    staff = web.client("staff")
    p = make_product(staff, "OVER", "10.00", 5)
    r = place(staff, [{"product_id": p["id"], "quantity": 6}], "six")
    assert r.status_code == 409
    d = r.json()["error"]["details"][0]
    assert (d["requested"], d["available"]) == (6, 5) and stock(staff, p["id"]) == 5


def test_a_product_created_with_no_stock_cannot_be_ordered(web):
    staff = web.client("staff")
    p = make_product(staff, "EMPTY", "10.00", 0)
    r = place(staff, [{"product_id": p["id"], "quantity": 1}], "empty")
    assert r.status_code == 409 and r.json()["error"]["details"][0]["available"] == 0


def test_a_shortage_on_one_line_takes_nothing_from_the_others(web, pool):
    staff = web.client("staff")
    plenty, scarce = make_product(staff, "PLENTY", "10.00", 100), make_product(staff, "SCARCE", "10.00", 1)
    r = place(staff, [{"product_id": plenty["id"], "quantity": 10}, {"product_id": scarce["id"], "quantity": 2}], "mix")
    assert r.status_code == 409
    assert [d["product_id"] for d in r.json()["error"]["details"]] == [scarce["id"]]      # only the short one is named
    assert stock(staff, plenty["id"]) == 100 and stock(staff, scarce["id"]) == 1
    assert scalar(pool, "SELECT count(*) FROM orders") == 0 and scalar(pool, "SELECT count(*) FROM idempotency_keys") == 0


def test_every_short_product_is_reported_together(web):
    staff = web.client("staff")
    a, b = make_product(staff, "SHORT-A", "1.00", 1), make_product(staff, "SHORT-B", "1.00", 2)
    r = place(staff, [{"product_id": a["id"], "quantity": 5}, {"product_id": b["id"], "quantity": 9}], "both")
    assert r.status_code == 409
    assert {d["product_id"]: (d["requested"], d["available"]) for d in r.json()["error"]["details"]} == {
        a["id"]: (5, 1), b["id"]: (9, 2)}


def test_repeated_lines_for_one_product_count_together(web):
    staff = web.client("staff")
    p = make_product(staff, "MERGE", "1.00", 5)
    r = place(staff, [{"product_id": p["id"], "quantity": 3}, {"product_id": p["id"], "quantity": 3}], "merge")
    assert r.status_code == 409 and r.json()["error"]["details"][0]["requested"] == 6      # 3 + 3, not 3 twice
    assert stock(staff, p["id"]) == 5


def test_a_refused_order_does_not_use_up_its_idempotency_key(web):
    staff = web.client("staff")
    p = make_product(staff, "RETRY", "10.00", 1)
    assert place(staff, [{"product_id": p["id"], "quantity": 3}], "same-key").status_code == 409
    assert staff.patch(f"/products/{p['id']}/stock", json={"set_to": 5}).status_code == 200
    assert place(staff, [{"product_id": p["id"], "quantity": 3}], "same-key").status_code == 201
    assert stock(staff, p["id"]) == 2


def test_stock_cannot_be_adjusted_below_zero(web):
    staff = web.client("staff")
    p = make_product(staff, "ADJ", "1.00", 3)
    r = staff.patch(f"/products/{p['id']}/stock", json={"adjust_by": -4})
    assert r.status_code == 409 and r.json()["error"]["code"] == "INSUFFICIENT_STOCK"
    assert r.json()["error"]["details"][0]["available"] == 3 and stock(staff, p["id"]) == 3
    assert staff.patch(f"/products/{p['id']}/stock", json={"adjust_by": -3}).json()["stock"] == 0
