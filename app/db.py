"""SQLite setup. Money is stored as integer cents so totals never suffer float rounding."""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sku          TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    name         TEXT    NOT NULL,
    price_cents  INTEGER NOT NULL CHECK (price_cents > 0),
    stock        INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    status       TEXT    NOT NULL,
    total_cents  INTEGER NOT NULL,
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS order_items (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id          INTEGER NOT NULL REFERENCES orders(id),
    product_id        INTEGER NOT NULL REFERENCES products(id),
    sku               TEXT    NOT NULL,
    name              TEXT    NOT NULL,
    unit_price_cents  INTEGER NOT NULL,
    quantity          INTEGER NOT NULL CHECK (quantity > 0),
    line_total_cents  INTEGER NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    # isolation_level=None -> we control transactions explicitly (see services.transaction).
    # check_same_thread=False -> FastAPI may open and use the connection on different worker threads;
    # it is still only ever used by one request at a time.
    conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: str) -> None:
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()
