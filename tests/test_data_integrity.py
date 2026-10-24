"""Ledger, notifications, and database-level safety nets."""
import psycopg
import pytest

from app import services
from tests.conftest import make_product, scalar
from tests.test_concurrency import order


def test_order_writes_items_ledger_and_notification_together(pool):
    p = make_product(pool, price="19.99", stock=10)
    outcome, result = order(pool, [(p["id"], 3)])
    assert outcome == "created" and result["total"] == "59.97"
    assert scalar(pool, "SELECT count(*) FROM order_items WHERE order_id = %s", (result["id"],)) == 1
    with pool.connection() as conn:
        moves = services.list_movements(conn, p["id"], 50, 0)["items"]
    assert [(m["change"], m["stock_after"], m["reason"]) for m in moves] == [
        (10, 10, "initial_stock"), (-3, 7, "order")]
    assert moves[1]["order_id"] == result["id"]
    assert scalar(pool, "SELECT status FROM notifications WHERE type = 'order_confirmed'") == "pending"


def test_failed_order_leaves_no_trace(pool):
    p = make_product(pool, stock=2)
    assert order(pool, [(p["id"], 5)])[0] == "INSUFFICIENT_STOCK"
    assert scalar(pool, "SELECT count(*) FROM orders") == 0
    assert scalar(pool, "SELECT count(*) FROM idempotency_keys") == 0
    assert scalar(pool, "SELECT count(*) FROM notifications WHERE type = 'order_confirmed'") == 0
    assert scalar(pool, "SELECT count(*) FROM inventory_movements") == 1     # only the initial stock


def test_low_stock_alert_fires_once_when_crossing_threshold(pool):
    p = make_product(pool, stock=8, threshold=5)
    order(pool, [(p["id"], 2)])     # 8 -> 6  (above threshold)
    assert scalar(pool, "SELECT count(*) FROM notifications WHERE type = 'low_stock'") == 0
    order(pool, [(p["id"], 1)])     # 6 -> 5  (crosses: alert)
    order(pool, [(p["id"], 1)])     # 5 -> 4  (already low: no second alert)
    assert scalar(pool, "SELECT count(*) FROM notifications WHERE type = 'low_stock'") == 1


def test_manual_stock_changes_are_recorded(pool):
    p = make_product(pool, stock=10)
    with pool.connection() as conn:
        services.update_stock(conn, p["id"], adjust_by=-4)
        services.update_stock(conn, p["id"], set_to=50)
        services.update_stock(conn, p["id"], set_to=50)      # no change -> no ledger row
        moves = services.list_movements(conn, p["id"], 50, 0)["items"]
        assert services.audit_inventory(conn)["consistent"]
    assert [(m["change"], m["reason"]) for m in moves] == [
        (10, "initial_stock"), (-4, "manual_adjustment"), (44, "manual_set")]


def test_database_refuses_negative_stock(pool):
    p = make_product(pool, stock=1)
    with pytest.raises(psycopg.errors.CheckViolation):
        with pool.connection() as conn:
            conn.execute("UPDATE products SET stock = -1 WHERE id = %s", (p["id"],))


def test_ledger_is_append_only(pool):
    p = make_product(pool, stock=3)
    with pytest.raises(psycopg.errors.RaiseException):
        with pool.connection() as conn:
            conn.execute("UPDATE inventory_movements SET change = 999 WHERE product_id = %s", (p["id"],))
    with pytest.raises(psycopg.errors.RaiseException):
        with pool.connection() as conn:
            conn.execute("DELETE FROM inventory_movements WHERE product_id = %s", (p["id"],))


def test_data_survives_reconnect(pool, db_url):
    """Persistence: a brand-new connection (like an app restart) sees the committed order."""
    p = make_product(pool, stock=10)
    outcome, result = order(pool, [(p["id"], 4)])
    with psycopg.connect(db_url) as fresh:
        stock_now = fresh.execute("SELECT stock FROM products WHERE id = %s", (p["id"],)).fetchone()[0]
        total = fresh.execute("SELECT total_cents FROM orders WHERE id = %s", (result["id"],)).fetchone()[0]
    assert stock_now == 6 and total == 4000
