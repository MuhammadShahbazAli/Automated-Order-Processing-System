"""Business rules on PostgreSQL (no FastAPI imports).

Concurrency strategy
--------------------
* Pessimistic row locks: an order runs `SELECT ... FOR UPDATE` on every product it touches,
  ordered by id. Two orders for the same product queue up; the second one re-reads the
  stock AFTER the first commits, so it sees the true remaining quantity.
* Locking in a fixed (id) order means two orders can never wait on each other (no deadlocks).
* `lock_timeout` makes a waiter give up (HTTP 503) instead of hanging forever.
* Safety nets in the database itself: CHECK (stock >= 0) and a guarded UPDATE (stock >= qty).
* Everything an order does (idempotency claim, stock, order, items, ledger, notifications)
  happens in ONE transaction: it all commits or none of it does.
"""
import hashlib
import json
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

from .config import LOCK_TIMEOUT
from .errors import AppError
from .limits import MAX_STOCK


# ---------- helpers ----------
def price_to_cents(price: Decimal) -> int:
    return int(Decimal(price).scaleb(2).to_integral_value())


def fmt_money(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def _set_lock_timeout(conn) -> None:
    conn.execute("SELECT set_config('lock_timeout', %s, true)", (LOCK_TIMEOUT,))  # true = this transaction only


def _product_out(row: dict) -> dict:
    return {
        "id": row["id"], "sku": row["sku"], "name": row["name"],
        "price": fmt_money(row["price_cents"]), "stock": row["stock"],
        "low_stock_threshold": row["low_stock_threshold"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _not_found(product_id: int) -> AppError:
    return AppError(404, "PRODUCT_NOT_FOUND", f"Product {product_id} does not exist.",
                    [{"field": "product_id", "product_id": product_id, "issue": "Product not found"}])


def _record_movement(conn, product_id: int, change: int, stock_after: int, reason: str,
                     order_id: int | None = None) -> None:
    conn.execute(
        "INSERT INTO inventory_movements (product_id, change, stock_after, reason, order_id) "
        "VALUES (%s, %s, %s, %s, %s)",
        (product_id, change, stock_after, reason, order_id),
    )


def _notify(conn, type_: str, payload: dict) -> None:
    conn.execute("INSERT INTO notifications (type, payload) VALUES (%s, %s)", (type_, Jsonb(payload)))


def _low_stock_check(conn, product: dict, previous: int, new: int) -> None:
    """One alert when stock CROSSES down to/below the threshold, not on every sale after it."""
    threshold = product["low_stock_threshold"]
    if previous > threshold >= new:
        _notify(conn, "low_stock", {"product_id": product["id"], "sku": product["sku"],
                                    "name": product["name"], "stock": new, "threshold": threshold})


# ---------- products ----------
def create_product(conn, sku: str, name: str, price: Decimal, stock: int, low_stock_threshold: int) -> dict:
    try:
        with conn.transaction():
            product_id = conn.execute(
                "INSERT INTO products (sku, name, price_cents, stock, low_stock_threshold) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (sku, name, price_to_cents(price), stock, low_stock_threshold),
            ).fetchone()["id"]
            if stock > 0:
                _record_movement(conn, product_id, stock, stock, "initial_stock")
    except psycopg.errors.UniqueViolation as exc:
        if exc.diag.constraint_name == "products_sku_lower_uq":
            raise AppError(409, "DUPLICATE_SKU", f"A product with SKU '{sku}' already exists.",
                           [{"field": "sku", "issue": "SKU must be unique"}]) from exc
        raise
    return get_product(conn, product_id)


def get_product(conn, product_id: int) -> dict:
    row = conn.execute("SELECT * FROM products WHERE id = %s", (product_id,)).fetchone()
    if row is None:
        raise _not_found(product_id)
    return _product_out(row)


def list_products(conn, limit: int, offset: int) -> dict:
    rows = conn.execute("SELECT * FROM products ORDER BY id LIMIT %s OFFSET %s", (limit, offset)).fetchall()
    total = conn.execute("SELECT count(*) AS n FROM products").fetchone()["n"]
    return {"items": [_product_out(r) for r in rows], "total": total, "limit": limit, "offset": offset}


# ---------- stock ----------
def get_stock(conn, product_id: int) -> dict:
    row = conn.execute("SELECT id, stock FROM products WHERE id = %s", (product_id,)).fetchone()
    if row is None:
        raise _not_found(product_id)
    return {"product_id": row["id"], "stock": row["stock"], "previous_stock": None}


def update_stock(conn, product_id: int, set_to: int | None = None, adjust_by: int | None = None) -> dict:
    with conn.transaction():
        _set_lock_timeout(conn)
        product = conn.execute(
            "SELECT id, sku, name, stock, low_stock_threshold FROM products WHERE id = %s FOR UPDATE",
            (product_id,),
        ).fetchone()
        if product is None:
            raise _not_found(product_id)
        previous = product["stock"]
        new = set_to if set_to is not None else previous + adjust_by
        if new < 0:
            raise AppError(409, "INSUFFICIENT_STOCK",
                           f"Cannot reduce stock by {-adjust_by}; only {previous} in stock.",
                           [{"field": "adjust_by", "product_id": product_id, "available": previous,
                             "issue": "Adjustment would make stock negative"}])
        if new > MAX_STOCK:
            raise AppError(422, "VALIDATION_ERROR", f"Stock cannot exceed {MAX_STOCK}.",
                           [{"field": "adjust_by", "issue": f"Resulting stock would be {new}"}])
        if new != previous:
            conn.execute("UPDATE products SET stock = %s, updated_at = now() WHERE id = %s", (new, product_id))
            _record_movement(conn, product_id, new - previous, new,
                             "manual_set" if set_to is not None else "manual_adjustment")
            _low_stock_check(conn, product, previous, new)
    return {"product_id": product_id, "stock": new, "previous_stock": previous}


def list_movements(conn, product_id: int, limit: int, offset: int) -> dict:
    get_product(conn, product_id)  # 404 if unknown
    rows = conn.execute(
        "SELECT * FROM inventory_movements WHERE product_id = %s ORDER BY id LIMIT %s OFFSET %s",
        (product_id, limit, offset),
    ).fetchall()
    total = conn.execute("SELECT count(*) AS n FROM inventory_movements WHERE product_id = %s",
                         (product_id,)).fetchone()["n"]
    return {"items": rows, "total": total, "limit": limit, "offset": offset}


def audit_inventory(conn) -> dict:
    """Reconciliation: products.stock must equal the sum of its ledger. Should always be consistent."""
    rows = conn.execute(
        "SELECT p.id AS product_id, p.sku, p.stock, COALESCE(SUM(m.change), 0)::bigint AS ledger_total "
        "FROM products p LEFT JOIN inventory_movements m ON m.product_id = p.id "
        "GROUP BY p.id HAVING p.stock <> COALESCE(SUM(m.change), 0) ORDER BY p.id"
    ).fetchall()
    return {"consistent": not rows, "discrepancies": rows}


# ---------- notifications ----------
def list_notifications(conn, status: str | None, limit: int) -> dict:
    if status:
        rows = conn.execute("SELECT * FROM notifications WHERE status = %s ORDER BY id LIMIT %s",
                            (status, limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM notifications ORDER BY id LIMIT %s", (limit,)).fetchall()
    return {"items": rows}


# ---------- orders ----------
def create_order(conn, idempotency_key: str, items: list[tuple[int, int]]) -> tuple[dict, bool]:
    """Returns (order, replayed). replayed=True means this key was already used with the same
    request, so the ORIGINAL order is returned and nothing new happens."""
    wanted: dict[int, int] = {}
    for product_id, qty in items:                     # merge repeated product IDs
        wanted[product_id] = wanted.get(product_id, 0) + qty
    request_hash = hashlib.sha256(
        json.dumps(sorted(wanted.items()), separators=(",", ":")).encode()
    ).hexdigest()

    with conn.transaction():
        _set_lock_timeout(conn)

        # Claim the key. If another transaction is mid-flight with the same key, this INSERT waits
        # for it to finish: afterwards we either replay its order (it committed) or proceed (it rolled back).
        claimed = conn.execute(
            "INSERT INTO idempotency_keys (key, request_hash) VALUES (%s, %s) "
            "ON CONFLICT (key) DO NOTHING RETURNING key",
            (idempotency_key, request_hash),
        ).fetchone()

        if claimed is None:
            existing = conn.execute(
                "SELECT request_hash, order_id FROM idempotency_keys WHERE key = %s", (idempotency_key,)
            ).fetchone()
            if existing["request_hash"] != request_hash:
                raise AppError(422, "IDEMPOTENCY_KEY_REUSED",
                               "This Idempotency-Key was already used with a different request.",
                               [{"field": "Idempotency-Key", "issue": "Use a new key for a different order"}])
            if existing["order_id"] is None:  # defensive; cannot normally be observed
                raise AppError(409, "IDEMPOTENCY_KEY_IN_PROGRESS", "A request with this key is still processing.")
            order_id, replayed = existing["order_id"], True
        else:
            order_id, replayed = _place_order(conn, wanted, idempotency_key), False

    return get_order(conn, order_id), replayed


def _place_order(conn, wanted: dict[int, int], idempotency_key: str) -> int:
    """Runs inside create_order's transaction."""
    # Lock every product row we need, in id order (deadlock-free), and read the CURRENT stock.
    rows = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, sku, name, price_cents, stock, low_stock_threshold "
            "FROM products WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            (sorted(wanted),),
        )
    }

    missing = [pid for pid in wanted if pid not in rows]
    if missing:
        raise AppError(404, "PRODUCT_NOT_FOUND", "One or more products do not exist.",
                       [{"field": "product_id", "product_id": pid, "issue": "Product not found"}
                        for pid in missing])

    short = [pid for pid, qty in wanted.items() if rows[pid]["stock"] < qty]
    if short:
        raise AppError(409, "INSUFFICIENT_STOCK", "Not enough stock for one or more products.",
                       [{"field": "quantity", "product_id": pid, "requested": wanted[pid],
                         "available": rows[pid]["stock"], "issue": "Insufficient stock"} for pid in short])

    lines = [(rows[pid], qty, rows[pid]["price_cents"] * qty) for pid, qty in wanted.items()]
    total_cents = sum(line_cents for _, _, line_cents in lines)      # server-side total

    order_id = conn.execute(
        "INSERT INTO orders (status, total_cents) VALUES ('confirmed', %s) RETURNING id", (total_cents,)
    ).fetchone()["id"]

    for product, qty, line_cents in lines:
        updated = conn.execute(
            "UPDATE products SET stock = stock - %s, updated_at = now() "
            "WHERE id = %s AND stock >= %s RETURNING stock",
            (qty, product["id"], qty),
        ).fetchone()
        if updated is None:  # defensive: impossible while we hold the row lock
            raise AppError(409, "INSUFFICIENT_STOCK", "Stock changed while placing the order.",
                           [{"product_id": product["id"], "issue": "Insufficient stock"}])
        new_stock = updated["stock"]
        conn.execute(
            "INSERT INTO order_items (order_id, product_id, sku, name, unit_price_cents, quantity, "
            "line_total_cents) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (order_id, product["id"], product["sku"], product["name"], product["price_cents"], qty, line_cents),
        )
        _record_movement(conn, product["id"], -qty, new_stock, "order", order_id)
        _low_stock_check(conn, product, product["stock"], new_stock)

    _notify(conn, "order_confirmed", {"order_id": order_id, "total": fmt_money(total_cents),
                                      "line_count": len(lines)})
    conn.execute("UPDATE idempotency_keys SET order_id = %s WHERE key = %s", (order_id, idempotency_key))
    return order_id


def get_order(conn, order_id: int) -> dict:
    order = conn.execute("SELECT * FROM orders WHERE id = %s", (order_id,)).fetchone()
    if order is None:
        raise AppError(404, "ORDER_NOT_FOUND", f"Order {order_id} does not exist.",
                       [{"field": "order_id", "issue": "Order not found"}])
    lines = conn.execute("SELECT * FROM order_items WHERE order_id = %s ORDER BY id", (order_id,)).fetchall()
    return {
        "id": order["id"], "status": order["status"], "created_at": order["created_at"],
        "items": [{
            "product_id": l["product_id"], "sku": l["sku"], "name": l["name"],
            "unit_price": fmt_money(l["unit_price_cents"]), "quantity": l["quantity"],
            "line_total": fmt_money(l["line_total_cents"]),
        } for l in lines],
        "total": fmt_money(order["total_cents"]),
    }
