"""Integration tests run against a REAL PostgreSQL database.

    docker compose up -d db
    set TEST_DATABASE_URL=postgresql://inventory:inventory@localhost:5432/inventory_test   (see .env.example)
    pytest -v

Tests TRUNCATE every table, so the database name must end with "_test" (enforced below).
"""
import os
import uuid
from decimal import Decimal

import psycopg
import psycopg.conninfo
import pytest
from psycopg.rows import dict_row

from app import services
from app.db import create_pool
from app.migrate import run_migrations


@pytest.fixture(scope="session")
def db_url():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests (see tests/conftest.py)")
    dbname = psycopg.conninfo.conninfo_to_dict(url).get("dbname", "")
    if not dbname.endswith("_test"):
        pytest.exit(f"Refusing to run: database '{dbname}' does not end with '_test' (tests wipe all data).")
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        run_migrations(conn)
    return url


@pytest.fixture()
def pool(db_url):
    p = create_pool(db_url, min_size=2, max_size=30)
    p.open(wait=True)
    with p.connection() as conn:
        conn.execute("TRUNCATE order_status_history, inventory_movements, notifications, idempotency_keys, "
                     "order_items, orders, products RESTART IDENTITY CASCADE")
    yield p
    p.close()


def make_product(pool, sku="SKU-1", price="10.00", stock=10, threshold=5) -> dict:
    with pool.connection() as conn:
        return services.create_product(conn, sku, f"Item {sku}", Decimal(price), stock, threshold)


def scalar(pool, sql, params=()):
    with pool.connection() as conn:
        row = conn.execute(sql, params).fetchone()
        return next(iter(row.values()))


CUSTOMER = {"name": "Ayesha Khan", "email": "ayesha@example.com", "phone": "+92 321 1234567",
            "address": "12 Mall Road, Lahore"}


def place(pool, items, key=None, customer=CUSTOMER) -> dict:
    """Place an order through the service layer and return it (raises AppError on failure)."""
    with pool.connection() as conn:
        order, _ = services.create_order(conn, key or str(uuid.uuid4()), items, customer)
    return order
