"""All business rules live here (no FastAPI imports), so they are easy to test and reuse."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal

from .errors import AppError
from .limits import MAX_STOCK


# ---------- helpers ----------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def price_to_cents(price: Decimal) -> int:
    return int(Decimal(price).scaleb(2).to_integral_value())


def fmt_money(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


@contextmanager
def transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE takes the write lock up front, so check-then-update is race-free."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _product_out(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "sku": row["sku"],
        "name": row["name"],
        "price": fmt_money(row["price_cents"]),
        "stock": row["stock"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _not_found(product_id: int) -> AppError:
    return AppError(404, "PRODUCT_NOT_FOUND", f"Product {product_id} does not exist.",
                    [{"field": "product_id", "product_id": product_id, "issue": "Product not found"}])


# ---------- products ----------
def create_product(conn, sku: str, name: str, price: Decimal, stock: int) -> dict:
    now = _now()
    try:
        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO products (sku, name, price_cents, stock, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (sku, name, price_to_cents(price), stock, now, now),
            )
            product_id = cur.lastrowid
    except sqlite3.IntegrityError as exc:
        if "UNIQUE" in str(exc):
            raise AppError(409, "DUPLICATE_SKU", f"A product with SKU '{sku}' already exists.",
                           [{"field": "sku", "issue": "SKU must be unique"}]) from exc
        raise
    return get_product(conn, product_id)


def get_product(conn, product_id: int) -> dict:
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if row is None:
        raise _not_found(product_id)
    return _product_out(row)


def list_products(conn, limit: int, offset: int) -> dict:
    rows = conn.execute("SELECT * FROM products ORDER BY id LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    return {"items": [_product_out(r) for r in rows], "total": total, "limit": limit, "offset": offset}


# ---------- stock ----------
def get_stock(conn, product_id: int) -> dict:
    row = conn.execute("SELECT id, stock FROM products WHERE id = ?", (product_id,)).fetchone()
    if row is None:
        raise _not_found(product_id)
    return {"product_id": row["id"], "stock": row["stock"], "previous_stock": None}


def update_stock(conn, product_id: int, set_to: int | None = None, adjust_by: int | None = None) -> dict:
    with transaction(conn):
        row = conn.execute("SELECT stock FROM products WHERE id = ?", (product_id,)).fetchone()
        if row is None:
            raise _not_found(product_id)
        previous = row["stock"]
        new = set_to if set_to is not None else previous + adjust_by
        if new < 0:
            raise AppError(409, "INSUFFICIENT_STOCK",
                           f"Cannot reduce stock by {-adjust_by}; only {previous} in stock.",
                           [{"field": "adjust_by", "product_id": product_id,
                             "available": previous, "issue": "Adjustment would make stock negative"}])
        if new > MAX_STOCK:
            raise AppError(422, "VALIDATION_ERROR", f"Stock cannot exceed {MAX_STOCK}.",
                           [{"field": "adjust_by", "issue": f"Resulting stock would be {new}"}])
        conn.execute("UPDATE products SET stock = ?, updated_at = ? WHERE id = ?", (new, _now(), product_id))
    return {"product_id": product_id, "stock": new, "previous_stock": previous}


# ---------- orders ----------
def create_order(conn, items: list[tuple[int, int]]) -> dict:
    """items: [(product_id, quantity), ...]. All-or-nothing: either every line is
    fulfilled and stock is deducted, or nothing changes. Repeated product IDs are merged."""
    wanted: dict[int, int] = {}
    for product_id, qty in items:
        wanted[product_id] = wanted.get(product_id, 0) + qty

    with transaction(conn):
        marks = ",".join("?" * len(wanted))
        rows = {r["id"]: r for r in conn.execute(f"SELECT * FROM products WHERE id IN ({marks})", list(wanted))}

        missing = [pid for pid in wanted if pid not in rows]
        if missing:
            raise AppError(404, "PRODUCT_NOT_FOUND", "One or more products do not exist.",
                           [{"field": "product_id", "product_id": pid, "issue": "Product not found"}
                            for pid in missing])

        short = [pid for pid, qty in wanted.items() if rows[pid]["stock"] < qty]
        if short:
            raise AppError(409, "INSUFFICIENT_STOCK", "Not enough stock for one or more products.",
                           [{"field": "quantity", "product_id": pid, "requested": wanted[pid],
                             "available": rows[pid]["stock"], "issue": "Insufficient stock"}
                            for pid in short])

        now = _now()
        lines = []
        total_cents = 0
        for pid, qty in wanted.items():
            p = rows[pid]
            line_cents = p["price_cents"] * qty          # server-side calculation
            total_cents += line_cents
            lines.append((p, qty, line_cents))

        order_id = conn.execute(
            "INSERT INTO orders (status, total_cents, created_at) VALUES ('confirmed', ?, ?)",
            (total_cents, now),
        ).lastrowid

        for p, qty, line_cents in lines:
            cur = conn.execute(
                "UPDATE products SET stock = stock - ?, updated_at = ? WHERE id = ? AND stock >= ?",
                (qty, now, p["id"], qty),
            )
            if cur.rowcount != 1:  # defensive: should be impossible under the write lock
                raise AppError(409, "INSUFFICIENT_STOCK", "Stock changed while placing the order.",
                               [{"product_id": p["id"], "issue": "Insufficient stock"}])
            conn.execute(
                "INSERT INTO order_items (order_id, product_id, sku, name, unit_price_cents, quantity, "
                "line_total_cents) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (order_id, p["id"], p["sku"], p["name"], p["price_cents"], qty, line_cents),
            )

    return get_order(conn, order_id)


def get_order(conn, order_id: int) -> dict:
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        raise AppError(404, "ORDER_NOT_FOUND", f"Order {order_id} does not exist.",
                       [{"field": "order_id", "issue": "Order not found"}])
    lines = conn.execute("SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (order_id,)).fetchall()
    return {
        "id": order["id"],
        "status": order["status"],
        "created_at": order["created_at"],
        "items": [{
            "product_id": l["product_id"], "sku": l["sku"], "name": l["name"],
            "unit_price": fmt_money(l["unit_price_cents"]), "quantity": l["quantity"],
            "line_total": fmt_money(l["line_total_cents"]),
        } for l in lines],
        "total": fmt_money(order["total_cents"]),
    }
