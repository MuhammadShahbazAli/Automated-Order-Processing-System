"""The Phase 2 guarantee: simultaneous requests can never oversell stock."""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from app import services
from app.errors import AppError
from tests.conftest import make_product, scalar


def fire(pool, n, work):
    """Run work(i) in n threads that all start at the same instant."""
    barrier = threading.Barrier(n)

    def run(i):
        barrier.wait()
        return work(i)

    with ThreadPoolExecutor(max_workers=n) as ex:
        return list(ex.map(run, range(n)))


def order(pool, items, key=None):
    try:
        with pool.connection() as conn:
            result, replayed = services.create_order(conn, key or str(uuid.uuid4()), items)
        return "replayed" if replayed else "created", result
    except AppError as exc:
        return exc.code, None


def assert_consistent(pool):
    with pool.connection() as conn:
        audit = services.audit_inventory(conn)
    assert audit["consistent"], audit["discrepancies"]
    assert scalar(pool, "SELECT count(*) FROM products WHERE stock < 0") == 0


def test_simultaneous_orders_never_oversell(pool):
    p = make_product(pool, stock=10)
    results = fire(pool, 25, lambda i: order(pool, [(p["id"], 1)]))
    outcomes = [r[0] for r in results]
    assert outcomes.count("created") == 10
    assert outcomes.count("INSUFFICIENT_STOCK") == 15
    assert scalar(pool, "SELECT stock FROM products WHERE id = %s", (p["id"],)) == 0
    assert scalar(pool, "SELECT count(*) FROM orders") == 10
    assert scalar(pool, "SELECT count(*) FROM inventory_movements") == 11   # 1 initial + 10 orders
    assert_consistent(pool)


def test_larger_orders_only_fill_what_fits(pool):
    p = make_product(pool, stock=10)
    results = fire(pool, 25, lambda i: order(pool, [(p["id"], 3)]))
    assert [r[0] for r in results].count("created") == 3        # 3 x 3 = 9, the 4th would need 12
    assert scalar(pool, "SELECT stock FROM products WHERE id = %s", (p["id"],)) == 1
    assert_consistent(pool)


def test_opposite_item_order_does_not_deadlock(pool):
    a = make_product(pool, "A", stock=100)
    b = make_product(pool, "B", stock=100)

    def work(i):
        items = [(a["id"], 1), (b["id"], 1)] if i % 2 == 0 else [(b["id"], 1), (a["id"], 1)]
        return order(pool, items)

    results = fire(pool, 24, work)
    assert [r[0] for r in results] == ["created"] * 24
    assert scalar(pool, "SELECT stock FROM products WHERE id = %s", (a["id"],)) == 76
    assert scalar(pool, "SELECT stock FROM products WHERE id = %s", (b["id"],)) == 76
    assert_consistent(pool)


def test_stock_adjustments_and_orders_interleave_safely(pool):
    p = make_product(pool, stock=20)

    def work(i):
        if i % 2 == 0:
            return order(pool, [(p["id"], 1)])[0]
        try:
            with pool.connection() as conn:
                services.update_stock(conn, p["id"], adjust_by=-1)
            return "adjusted"
        except AppError as exc:
            return exc.code

    results = fire(pool, 20, work)
    assert sorted(set(results)) == ["adjusted", "created"]       # 10 + 10 = exactly the 20 in stock
    assert scalar(pool, "SELECT stock FROM products WHERE id = %s", (p["id"],)) == 0
    assert_consistent(pool)


def test_overselling_attempt_across_products_is_all_or_nothing(pool):
    a = make_product(pool, "A", stock=100)
    b = make_product(pool, "B", stock=5)
    results = fire(pool, 20, lambda i: order(pool, [(a["id"], 1), (b["id"], 1)]))
    outcomes = [r[0] for r in results]
    assert outcomes.count("created") == 5
    assert outcomes.count("INSUFFICIENT_STOCK") == 15
    # Failed orders must not have taken anything from A either.
    assert scalar(pool, "SELECT stock FROM products WHERE id = %s", (a["id"],)) == 95
    assert scalar(pool, "SELECT stock FROM products WHERE id = %s", (b["id"],)) == 0
    assert_consistent(pool)
