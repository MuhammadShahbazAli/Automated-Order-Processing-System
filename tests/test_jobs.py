"""Scheduled jobs: daily report and housekeeping (needs TEST_DATABASE_URL)."""
from datetime import time
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from app import jobs, services
from tests.conftest import make_product, place, scalar
from tests.test_concurrency import fire
from tests.test_worker import FakeBackend, SENDER

SETTINGS = jobs.Settings(recipients=("boss@example.com", "owner@example.com"))
KARACHI = ZoneInfo("Asia/Karachi")


def ensure(pool, settings=SETTINGS):
    with pool.connection() as conn:
        jobs.ensure_schedules(conn, settings)


def run(pool, settings=SETTINGS):
    with pool.connection() as conn:
        return jobs.run_one_schedule(conn, settings)


def make_due(pool, name):
    with pool.connection() as conn:
        conn.execute("UPDATE schedules SET next_run_at = now() WHERE name = %s", (name,))


def schedule(pool, name):
    with pool.connection() as conn:
        return conn.execute("SELECT * FROM schedules WHERE name = %s", (name,)).fetchone()


def reports(pool):
    with pool.connection() as conn:
        return conn.execute("SELECT * FROM reports ORDER BY id").fetchall()


def test_schedules_are_created_for_the_future_and_are_idempotent(pool):
    ensure(pool)
    ensure(pool)
    assert scalar(pool, "SELECT count(*) FROM schedules") == 2
    daily, clean = schedule(pool, "daily_report"), schedule(pool, "housekeeping")
    assert daily["next_run_at"].astimezone(KARACHI).time() == time(8, 0)
    assert clean["next_run_at"].astimezone(KARACHI).time() == time(3, 0)
    assert scalar(pool, "SELECT next_run_at > now() FROM schedules WHERE name = 'daily_report'") is True

    ensure(pool, jobs.Settings(report_time=time(9, 30), recipients=SETTINGS.recipients))   # config changed
    assert schedule(pool, "daily_report")["next_run_at"].astimezone(KARACHI).time() == time(9, 30)


def test_nothing_runs_before_its_time(pool):
    ensure(pool)
    assert run(pool) is False
    assert scalar(pool, "SELECT count(*) FROM reports") == 0


def test_daily_report_contents_and_delivery_queue(pool):
    a = make_product(pool, "A", "100.00", 10)
    b = make_product(pool, "B", "50.00", 3)
    place(pool, [(a["id"], 2)])
    place(pool, [(a["id"], 1), (b["id"], 1)])
    cancelled = place(pool, [(b["id"], 1)])
    with pool.connection() as conn:
        services.transition_order(conn, cancelled["id"], "cancelled", "test")

    ensure(pool)
    make_due(pool, "daily_report")
    assert run(pool) is True
    assert run(pool) is False                       # next_run_at moved forward: it does not run twice

    [report] = reports(pool)
    d = report["data"]
    assert d["orders"] == {"placed": 3, "by_status": {"confirmed": 2, "cancelled": 1},
                           "net_revenue": "350.00", "cancelled_in_period": 1}
    assert d["top_products"][0] == {"sku": "A", "name": "Item A", "quantity": 3, "revenue": "300.00"}
    assert d["top_products"][1]["sku"] == "B" and d["top_products"][1]["quantity"] == 1
    assert d["stock"]["products"] == 2 and d["stock"]["out_of_stock"] == 0 and d["stock"]["low_stock"] == 1
    assert d["stock"]["low_stock_items"] == [
        {"id": b["id"], "sku": "B", "name": "Item B", "stock": 2, "low_stock_threshold": 5}]
    assert d["audit"] == {"consistent": True, "discrepancies": []}
    assert d["delivery"]["pending"] == 4            # 3 order_confirmed + 1 order_cancelled, before the report mails

    with pool.connection() as conn:
        mails = conn.execute("SELECT recipient, payload FROM notifications WHERE type = 'daily_report' "
                             "ORDER BY id").fetchall()
    assert [m["recipient"] for m in mails] == ["boss@example.com", "owner@example.com"]
    assert all(m["payload"] == {"report_id": report["id"]} for m in mails)

    s = schedule(pool, "daily_report")
    assert (s["last_status"], s["consecutive_failures"], s["last_error"]) == ("ok", 0, None)
    assert scalar(pool, "SELECT next_run_at > now() FROM schedules WHERE name = 'daily_report'") is True
    assert scalar(pool, "SELECT count(*) FROM schedule_runs WHERE status = 'ok'") == 1


def test_consecutive_reports_leave_no_gap_and_no_overlap(pool):
    ensure(pool)
    make_due(pool, "daily_report")
    run(pool)
    make_due(pool, "daily_report")
    run(pool)
    first, second = reports(pool)
    assert second["period_start"] == first["period_end"]


def test_a_missed_schedule_catches_up_once(pool):
    ensure(pool)
    with pool.connection() as conn:
        conn.execute("UPDATE schedules SET next_run_at = now() - interval '3 days' WHERE name = 'daily_report'")
    assert run(pool) is True
    assert run(pool) is False                       # one catch-up run, not three
    assert len(reports(pool)) == 1
    assert scalar(pool, "SELECT next_run_at > now() FROM schedules WHERE name = 'daily_report'") is True


def test_only_one_worker_runs_a_due_schedule(pool):
    ensure(pool)
    make_due(pool, "daily_report")
    results = fire(pool, 4, lambda i: run(pool))
    assert results.count(True) == 1
    assert len(reports(pool)) == 1
    assert scalar(pool, "SELECT count(*) FROM notifications WHERE type = 'daily_report'") == 2


def test_failed_job_is_recorded_rolled_back_and_retried_soon(pool, monkeypatch):
    def boom(conn, now, settings):
        conn.execute("INSERT INTO reports (kind, period_start, period_end, data) "
                     "VALUES ('daily_report', now(), now(), %s)", (Jsonb({}),))
        raise RuntimeError("database exploded")

    ensure(pool)
    make_due(pool, "daily_report")
    monkeypatch.setitem(jobs.HANDLERS, "daily_report", boom)
    assert run(pool) is True

    assert scalar(pool, "SELECT count(*) FROM reports") == 0                 # the half-done work is gone
    s = schedule(pool, "daily_report")
    assert s["last_status"] == "failed" and "database exploded" in s["last_error"]
    assert s["consecutive_failures"] == 1
    assert scalar(pool, "SELECT next_run_at > now() AND next_run_at <= now() + interval '10 minutes' "
                        "FROM schedules WHERE name = 'daily_report'") is True    # retried soon, not tomorrow
    assert scalar(pool, "SELECT count(*) FROM schedule_runs WHERE status = 'failed'") == 1
    assert "database exploded" in scalar(pool, "SELECT error FROM schedule_runs WHERE status = 'failed'")

    monkeypatch.undo()                                                       # the problem is fixed
    make_due(pool, "daily_report")
    assert run(pool) is True
    s = schedule(pool, "daily_report")
    assert (s["last_status"], s["consecutive_failures"], s["last_error"]) == ("ok", 0, None)
    assert len(reports(pool)) == 1


def test_housekeeping_removes_only_old_finished_rows(pool):
    p = make_product(pool, stock=50)
    for key in ("K1", "K2", "K3"):
        place(pool, [(p["id"], 1)], key=key)
    with pool.connection() as conn:
        conn.execute("UPDATE idempotency_keys SET created_at = now() - interval '40 days' WHERE key = 'K1'")
        conn.execute("UPDATE notifications SET status = 'sent', created_at = now() - interval '100 days' WHERE id = 1")
        conn.execute("UPDATE notifications SET status = 'failed', created_at = now() - interval '100 days' WHERE id = 2")
        conn.execute("UPDATE notifications SET created_at = now() - interval '100 days' WHERE id = 3")  # still pending
    ensure(pool)
    make_due(pool, "housekeeping")
    assert run(pool) is True

    with pool.connection() as conn:
        keys = {r["key"] for r in conn.execute("SELECT key FROM idempotency_keys")}
        ids = {r["id"] for r in conn.execute("SELECT id FROM notifications")}
    assert keys == {"K2", "K3"}
    assert ids == {2, 3}                            # old sent one removed; failed and pending are kept
    assert "1 old idempotency key" in scalar(pool, "SELECT detail FROM schedule_runs WHERE status = 'ok'")


def test_report_email_reaches_every_recipient(pool):
    from app import worker
    p = make_product(pool, stock=10)
    place(pool, [(p["id"], 1)])
    ensure(pool)
    make_due(pool, "daily_report")
    run(pool)
    backend = FakeBackend()
    with pool.connection() as conn:
        while worker.process_one(conn, backend, SENDER, KARACHI):
            pass
    reports_sent = [m for _, m in backend.sent if m["Subject"].startswith("Daily report")]
    assert sorted(m["To"] for m in reports_sent) == ["boss@example.com", "owner@example.com"]
    body = reports_sent[0].get_content()
    assert "Net revenue" in body and "Placed: 1 (1 confirmed)" in body
