"""Integration tests run against a REAL PostgreSQL database.

    docker compose up -d db
    set TEST_DATABASE_URL=postgresql://inventory:inventory@localhost:5432/inventory_test   (see .env.example)
    pytest -v

Tests TRUNCATE every table, so the database name must end with "_test" (enforced below).
"""
import os
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal

import psycopg
import psycopg.conninfo
import pytest
from psycopg.rows import dict_row

from fastapi.testclient import TestClient

from app import auth, services
from app.db import create_pool
from app.main import create_app
from app.migrate import run_migrations
from app.settings import Settings


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
        conn.execute("TRUNCATE security_events, sessions, users, notification_attempts, reports, schedule_runs, "
                     "schedules, worker_heartbeats, order_status_history, inventory_movements, notifications, "
                     "idempotency_keys, order_items, orders, products RESTART IDENTITY CASCADE")
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


# ---------------------------------------------------------------- authenticated web harness
DEFAULT_PASSWORD = "Correct-Horse-Battery-9"


def make_settings(**overrides) -> Settings:
    """Settings for tests: rate limits so high they never interfere unless a test lowers them."""
    base = dict(app_env="test", rate_ip="1000000/minute", rate_read="1000000/minute",
                rate_write="1000000/minute", rate_login="1000000/minute", log_level="WARNING", log_format="text",
                docs_enabled=True)
    base.update(overrides)
    return Settings(**base)


def make_user(pool, role="staff", email=None, password=DEFAULT_PASSWORD, name="Test User") -> dict:
    email = email or f"{role}-{uuid.uuid4().hex[:8]}@example.com"
    with pool.connection() as conn:
        user = auth.create_user(conn, email, name, password, role)
    return {**user, "password": password}


@contextmanager
def running_app(db_url, **kwargs):
    """The real application (lifespan, middleware, routes) on the test database, without a background worker."""
    settings = kwargs.pop("settings", None) or make_settings()
    app = create_app(db_url, auto_migrate=False, run_worker=False, settings=settings, **kwargs)
    with TestClient(app):
        yield app


class WebHarness:
    def __init__(self, app, pool):
        self.app, self.pool = app, pool

    def anon(self) -> TestClient:
        return TestClient(self.app)

    def client(self, role="staff", **user_kwargs) -> TestClient:
        """A new user with `role`, already signed in. Extra attributes: .user (incl. password), .token."""
        user = make_user(self.pool, role, **user_kwargs)
        client = TestClient(self.app)
        response = client.post("/auth/login", json={"email": user["email"], "password": user["password"]})
        assert response.status_code == 200, response.text
        client.token = response.json()["access_token"]
        client.user = user
        client.headers["Authorization"] = f"Bearer {client.token}"
        return client


@pytest.fixture()
def web(db_url, pool):
    with running_app(db_url) as app:
        yield WebHarness(app, pool)


@pytest.fixture()
def client(web):
    return web.client("admin")


@pytest.fixture()
def live_server(db_url, pool):
    """A real uvicorn server on a free port, for tests that need genuine simultaneous HTTP requests."""
    import uvicorn
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    app = create_app(db_url, auto_migrate=False, run_worker=False, settings=make_settings())
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "the test server did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
