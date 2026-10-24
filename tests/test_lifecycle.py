"""Order lifecycle: status transitions, cancellation, stock restoration (needs TEST_DATABASE_URL)."""
import pytest

from app import services
from app.errors import AppError
from tests.conftest import make_product, place, scalar
from tests.test_concurrency import assert_consistent, fire


def move(pool, order_id, to, note=None):
    with pool.connection() as conn:
        return services.transition_order(conn, order_id, to, note)


def stock(pool, product_id):
    return scalar(pool, "SELECT stock FROM products WHERE id = %s", (product_id,))


def status(pool, order_id):
    return scalar(pool, "SELECT status FROM orders WHERE id = %s", (order_id,))


def test_order_moves_from_submission_to_completion(pool):
    p = make_product(pool, stock=10)
    order = place(pool, [(p["id"], 2)])
    assert order["status"] == "confirmed" and order["invoice_number"] == f"INV-{order['id']:06d}"
    assert order["customer"]["email"] == "ayesha@example.com"
    for step in ("processing", "shipped", "completed"):
        assert move(pool, order["id"], step)["status"] == step
    with pool.connection() as conn:
        history = services.get_order_history(conn, order["id"])["items"]
        emails = [r for r in conn.execute(
            "SELECT type, recipient FROM notifications WHERE type <> 'low_stock' ORDER BY id")]
    assert [(h["from_status"], h["to_status"]) for h in history] == [
        (None, "confirmed"), ("confirmed", "processing"), ("processing", "shipped"), ("shipped", "completed")]
    assert [e["type"] for e in emails] == ["order_confirmed", "order_shipped", "order_completed"]
    assert {e["recipient"] for e in emails} == {"ayesha@example.com"}
    assert stock(pool, p["id"]) == 8            # a completed order keeps its stock deducted
    assert_consistent(pool)


def test_invalid_transitions_are_rejected_and_change_nothing(pool):
    p = make_product(pool, stock=50)
    fresh = place(pool, [(p["id"], 1)])["id"]
    for bad in ("shipped", "completed", "confirmed"):             # skipping steps / going nowhere
        with pytest.raises(AppError) as exc:
            move(pool, fresh, bad)
        assert (exc.value.status_code, exc.value.code) == (409, "INVALID_STATUS_TRANSITION")
        assert exc.value.details[0]["from"] == "confirmed"
    assert status(pool, fresh) == "confirmed"

    shipped = place(pool, [(p["id"], 1)])["id"]
    move(pool, shipped, "processing")
    move(pool, shipped, "shipped")
    with pytest.raises(AppError):                                  # too late to cancel
        move(pool, shipped, "cancelled")
    move(pool, shipped, "completed")
    for bad in ("cancelled", "processing"):                        # completed is final
        with pytest.raises(AppError):
            move(pool, shipped, bad)
    assert status(pool, shipped) == "completed"
    assert stock(pool, p["id"]) == 48
    assert_consistent(pool)


def test_cancel_restores_stock_and_writes_ledger(pool):
    p = make_product(pool, stock=10)
    order = place(pool, [(p["id"], 3)])
    assert stock(pool, p["id"]) == 7
    done = move(pool, order["id"], "cancelled", "Customer changed mind")
    assert done["status"] == "cancelled" and done["cancel_reason"] == "Customer changed mind"
    assert done["cancelled_at"] is not None
    assert stock(pool, p["id"]) == 10
    with pool.connection() as conn:
        moves = services.list_movements(conn, p["id"], 50, 0)["items"]
    assert [(m["change"], m["stock_after"], m["reason"]) for m in moves] == [
        (10, 10, "initial_stock"), (-3, 7, "order"), (3, 10, "order_cancelled")]
    assert moves[2]["order_id"] == order["id"]
    assert scalar(pool, "SELECT type FROM notifications WHERE type = 'order_cancelled'") == "order_cancelled"
    assert_consistent(pool)


def test_cancel_from_processing_and_multi_product_restore(pool):
    a, b = make_product(pool, "A", stock=10), make_product(pool, "B", stock=5)
    order = place(pool, [(a["id"], 4), (b["id"], 5)])
    move(pool, order["id"], "processing")
    move(pool, order["id"], "cancelled")
    assert (stock(pool, a["id"]), stock(pool, b["id"])) == (10, 5)
    assert_consistent(pool)


def test_cancelling_twice_restores_stock_only_once(pool):
    p = make_product(pool, stock=10)
    order = place(pool, [(p["id"], 3)])
    move(pool, order["id"], "cancelled")
    with pytest.raises(AppError) as exc:
        move(pool, order["id"], "cancelled")
    assert exc.value.code == "INVALID_STATUS_TRANSITION"
    assert stock(pool, p["id"]) == 10
    assert scalar(pool, "SELECT count(*) FROM inventory_movements WHERE reason = 'order_cancelled'") == 1


def test_cancelled_stock_can_be_sold_again(pool):
    p = make_product(pool, stock=1)
    first = place(pool, [(p["id"], 1)])
    with pytest.raises(AppError):                                  # sold out
        place(pool, [(p["id"], 1)])
    move(pool, first["id"], "cancelled")
    assert place(pool, [(p["id"], 1)])["status"] == "confirmed"
    assert stock(pool, p["id"]) == 0
    assert_consistent(pool)


def test_simultaneous_cancels_restore_stock_exactly_once(pool):
    p = make_product(pool, stock=10)
    order = place(pool, [(p["id"], 3)])

    def cancel(_):
        try:
            move(pool, order["id"], "cancelled")
            return "cancelled"
        except AppError as exc:
            return exc.code

    results = fire(pool, 10, cancel)
    assert results.count("cancelled") == 1
    assert results.count("INVALID_STATUS_TRANSITION") == 9
    assert stock(pool, p["id"]) == 10
    assert scalar(pool, "SELECT count(*) FROM inventory_movements WHERE reason = 'order_cancelled'") == 1
    assert_consistent(pool)


def test_cancel_racing_with_shipping_has_one_winner(pool):
    p = make_product(pool, stock=10)
    order = place(pool, [(p["id"], 3)])
    move(pool, order["id"], "processing")

    def work(i):
        try:
            move(pool, order["id"], "cancelled" if i % 2 else "shipped")
            return "ok"
        except AppError:
            return "rejected"

    results = fire(pool, 10, work)
    assert results.count("ok") == 1
    final = status(pool, order["id"])
    assert final in ("shipped", "cancelled")
    assert stock(pool, p["id"]) == (10 if final == "cancelled" else 7)
    assert_consistent(pool)


def test_unknown_order_is_404(pool):
    with pytest.raises(AppError) as exc:
        move(pool, 999, "processing")
    assert (exc.value.status_code, exc.value.code) == (404, "ORDER_NOT_FOUND")


def test_customer_details_are_stored_normalized(pool):
    p = make_product(pool, stock=5)
    order = place(pool, [(p["id"], 1)], customer={"name": " Sara Ali ", "email": "Sara@Example.COM",
                                                  "phone": "", "address": None})
    assert order["customer"] == {"name": "Sara Ali", "email": "sara@example.com", "phone": None, "address": None}


def test_same_key_with_a_different_customer_is_a_different_request(pool):
    p = make_product(pool, stock=5)
    place(pool, [(p["id"], 1)], key="K", customer={"name": "A", "email": "a@example.com"})
    with pytest.raises(AppError) as exc:
        place(pool, [(p["id"], 1)], key="K", customer={"name": "B", "email": "b@example.com"})
    assert exc.value.code == "IDEMPOTENCY_KEY_REUSED"


def test_internal_orders_without_customer_still_work(pool):
    p = make_product(pool, stock=5)
    with pool.connection() as conn:
        order, _ = services.create_order(conn, "no-customer", [(p["id"], 1)])
    assert order["customer"] is None
    assert scalar(pool, "SELECT recipient FROM notifications WHERE type = 'order_confirmed'") is None
