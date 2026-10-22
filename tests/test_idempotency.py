"""Retries and double-clicks must never create duplicate orders."""
from app import services
from app.errors import AppError
from tests.conftest import make_product, scalar
from tests.test_concurrency import fire, order


def stock(pool, pid):
    return scalar(pool, "SELECT stock FROM products WHERE id = %s", (pid,))


def test_same_key_twice_returns_same_order(pool):
    p = make_product(pool, stock=10)
    first = order(pool, [(p["id"], 2)], key="K1")
    second = order(pool, [(p["id"], 2)], key="K1")
    assert first[0] == "created" and second[0] == "replayed"
    assert first[1]["id"] == second[1]["id"]
    assert stock(pool, p["id"]) == 8                     # deducted once, not twice
    assert scalar(pool, "SELECT count(*) FROM orders") == 1
    assert scalar(pool, "SELECT count(*) FROM notifications WHERE type = 'order_confirmed'") == 1


def test_simultaneous_duplicates_create_exactly_one_order(pool):
    p = make_product(pool, stock=10)
    results = fire(pool, 20, lambda i: order(pool, [(p["id"], 2)], key="SAME-KEY"))
    outcomes = [r[0] for r in results]
    assert outcomes.count("created") == 1
    assert outcomes.count("replayed") == 19
    assert len({r[1]["id"] for r in results}) == 1       # everyone got the same order
    assert stock(pool, p["id"]) == 8
    assert scalar(pool, "SELECT count(*) FROM orders") == 1


def test_same_key_different_request_is_rejected(pool):
    p = make_product(pool, stock=10)
    assert order(pool, [(p["id"], 1)], key="K2")[0] == "created"
    assert order(pool, [(p["id"], 2)], key="K2")[0] == "IDEMPOTENCY_KEY_REUSED"
    assert stock(pool, p["id"]) == 9


def test_failed_attempt_does_not_consume_the_key(pool):
    p = make_product(pool, stock=1)
    assert order(pool, [(p["id"], 5)], key="K3")[0] == "INSUFFICIENT_STOCK"
    with pool.connection() as conn:
        services.update_stock(conn, p["id"], set_to=10)
    outcome, result = order(pool, [(p["id"], 5)], key="K3")   # same key, now it can succeed
    assert outcome == "created"
    assert stock(pool, p["id"]) == 5


def test_repeated_product_lines_hash_the_same_as_merged_line(pool):
    p = make_product(pool, stock=10)
    assert order(pool, [(p["id"], 2), (p["id"], 3)], key="K4")[0] == "created"
    assert order(pool, [(p["id"], 5)], key="K4")[0] == "replayed"
    assert stock(pool, p["id"]) == 5
