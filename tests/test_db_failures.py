"""What happens when the database misbehaves (needs TEST_DATABASE_URL)."""
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import services
from app.main import create_app
from tests.conftest import WebHarness, make_product, make_settings, running_app, scalar
from tests.test_api import CUSTOMER, place


def counts(pool, *product_ids):
    return {
        "orders": scalar(pool, "SELECT count(*) FROM orders"),
        "keys": scalar(pool, "SELECT count(*) FROM idempotency_keys"),
        "confirmed_mails": scalar(pool, "SELECT count(*) FROM notifications WHERE type = 'order_confirmed'"),
        "stock": [scalar(pool, "SELECT stock FROM products WHERE id = %s", (pid,)) for pid in product_ids],
    }


def test_a_crash_in_the_middle_of_an_order_rolls_everything_back(web, pool, monkeypatch):
    staff = web.client("staff")
    a, b = make_product(pool, "FAIL-A", stock=10), make_product(pool, "FAIL-B", stock=10)
    items = [{"product_id": a["id"], "quantity": 2}, {"product_id": b["id"], "quantity": 3}]
    before = counts(pool, a["id"], b["id"])

    real, calls = services._record_movement, {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:                                   # the SECOND product line blows up
            raise RuntimeError("disk full: /var/lib/secret/path")
        return real(*args, **kwargs)

    monkeypatch.setattr(services, "_record_movement", flaky)
    r = place(staff, items, "crash-key")
    assert r.status_code == 500 and r.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "disk full" not in r.text and "secret" not in r.text and r.json()["error"]["request_id"]
    assert counts(pool, a["id"], b["id"]) == before          # no order, no stock taken, key NOT used up

    monkeypatch.undo()
    retry = place(staff, items, "crash-key")                  # the client simply retries with the same key
    assert retry.status_code == 201
    assert counts(pool, a["id"], b["id"])["stock"] == [8, 7]


def test_a_stuck_row_lock_gives_a_503_and_loses_nothing(web, pool, db_url, monkeypatch):
    monkeypatch.setattr(services, "LOCK_TIMEOUT", "300ms")
    staff = web.client("staff")
    p = make_product(pool, "LOCKED", stock=10)
    before = counts(pool, p["id"])
    holder = psycopg.connect(db_url, autocommit=False)        # another transaction holds the product row
    try:
        holder.execute("SELECT id FROM products WHERE id = %s FOR UPDATE", (p["id"],))
        r = place(staff, [{"product_id": p["id"], "quantity": 1}], "locked-key")
        assert r.status_code == 503 and r.json()["error"]["code"] == "BUSY"
        assert int(r.headers["retry-after"]) >= 1
        stuck_stock = staff.patch(f"/products/{p['id']}/stock", json={"adjust_by": 1})
        assert stuck_stock.status_code == 503                  # stock changes wait for the same lock
    finally:
        holder.rollback()
        holder.close()
    assert counts(pool, p["id"]) == before
    assert place(staff, [{"product_id": p["id"], "quantity": 1}], "locked-key").status_code == 201   # retry works


def test_the_api_recovers_after_every_database_connection_is_killed(web, pool):
    viewer = web.client("viewer")
    assert viewer.get("/products").status_code == 200
    with pool.connection() as conn:
        conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                     "WHERE datname = current_database() AND pid <> pg_backend_pid()")
    codes = [viewer.get("/products").status_code for _ in range(4)]
    assert set(codes) <= {200, 503}, f"unexpected status codes: {codes}"     # never a 500 or a stack trace
    assert codes[-1] == 200                                                   # and it heals by itself


def test_a_dead_connection_gives_a_503_with_no_internals(web, pool):
    viewer = web.client("viewer")
    with pool.connection() as conn:
        conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                     "WHERE datname = current_database() AND pid <> pg_backend_pid()")
    for _ in range(4):
        r = viewer.get("/products")
        if r.status_code == 503:
            assert r.json()["error"]["code"] == "DATABASE_UNAVAILABLE" and int(r.headers["retry-after"]) >= 1
            assert "psycopg" not in r.text.lower() and "terminating" not in r.text.lower()


def test_pool_exhaustion_is_a_503_not_a_hang(db_url, pool):
    with running_app(db_url, settings=make_settings(pool_timeout=0.5), pool_max=1) as app:
        client = WebHarness(app, pool).anon()
        with app.state.pool.connection():                      # the only connection is busy...
            r = client.get("/health")                          # ...so this request cannot get one
        assert r.status_code == 503 and r.json()["error"]["code"] == "BUSY"
        assert client.get("/health").status_code == 200        # and the service is fine once it is free


def test_startup_fails_fast_when_the_database_is_unreachable():
    app = create_app("postgresql://inventory:Str0ng-Secret-Value@127.0.0.1:1/inventory", auto_migrate=False,
                     run_worker=False, settings=make_settings())
    with pytest.raises(RuntimeError) as exc:
        with TestClient(app):
            pass
    assert "Cannot connect to PostgreSQL" in str(exc.value)
    assert "Str0ng-Secret-Value" not in str(exc.value)          # the password never appears in the error


def test_startup_fails_fast_with_a_wrong_password(db_url):
    parts = urlsplit(db_url)
    bad = urlunsplit(parts._replace(netloc=f"{parts.username}:definitely-wrong-pw@{parts.hostname}:{parts.port or 5432}"))
    app = create_app(bad, auto_migrate=False, run_worker=False, settings=make_settings())
    with pytest.raises(RuntimeError) as exc:
        with TestClient(app):
            pass
    assert "password authentication failed" in str(exc.value) and "definitely-wrong-pw" not in str(exc.value)


def test_unsafe_production_configuration_refuses_to_start(db_url):
    from app.settings import ConfigError, Settings
    weak = "postgresql://inventory:inventory@127.0.0.1:5432/inventory"
    with pytest.raises(ConfigError) as exc:
        create_app(weak, run_worker=False, settings=Settings(app_env="production", docs_enabled=False))
    assert "guessable" in str(exc.value)


def test_database_errors_in_one_request_do_not_poison_the_next(web, pool):
    staff = web.client("staff")
    p = make_product(pool, "NEXT", stock=3)
    assert staff.post("/orders", json={"items": [{"product_id": p["id"], "quantity": 9}], "customer": CUSTOMER},
                      headers={"Idempotency-Key": "a"}).status_code == 409
    assert staff.get("/products").status_code == 200
    assert place(staff, [{"product_id": p["id"], "quantity": 3}], "b").status_code == 201
