"""Failures are contained, recorded and retried safely (needs TEST_DATABASE_URL)."""
import smtplib
import threading
import time
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from app import jobs, ops, services, worker
from app.invoice import build_invoice_pdf
from tests.conftest import make_product, place, scalar
from tests.test_worker import SENDER, BrokenBackend, FakeBackend, drain, make_due, scalar_row


def attempts(pool, notification_id):
    with pool.connection() as conn:
        return ops.get_notification(conn, notification_id)["attempt_log"]


def test_email_failure_never_undoes_an_accepted_order(pool):
    p = make_product(pool, stock=10)
    order = place(pool, [(p["id"], 2)])
    for _ in range(worker.MAX_ATTEMPTS):                       # the mail server is down the whole time
        make_due(pool)
        drain(pool, BrokenBackend())

    assert scalar(pool, "SELECT status FROM notifications WHERE type = 'order_confirmed'") == "failed"
    log = attempts(pool, 1)
    assert len(log) == worker.MAX_ATTEMPTS
    assert all(a["outcome"] == "failed" and "smtp down" in a["error"] for a in log)
    assert [a["attempt"] for a in log] == list(range(1, worker.MAX_ATTEMPTS + 1))

    # ...and the order itself is untouched and fully usable.
    with pool.connection() as conn:
        current = services.get_order(conn, order["id"])
        assert current["status"] == "confirmed"
        assert services.transition_order(conn, order["id"], "processing")["status"] == "processing"
    assert scalar(pool, "SELECT stock FROM products WHERE id = %s", (p["id"],)) == 8
    assert build_invoice_pdf(current, {"name": "Shop", "address_lines": [], "currency": "PKR"}).startswith(b"%PDF")
    assert place(pool, [(p["id"], 1)])["status"] == "confirmed"          # new orders are still accepted


def test_orders_are_accepted_when_no_worker_is_running(pool):
    p = make_product(pool, stock=10)
    for _ in range(3):
        place(pool, [(p["id"], 1)])
    assert scalar(pool, "SELECT count(*) FROM notifications WHERE status = 'pending'") == 3
    assert scalar(pool, "SELECT count(*) FROM worker_heartbeats") == 0


def test_a_permanent_error_gives_up_at_once(pool):
    class RefusingBackend:
        def send(self, msg, name_hint):
            raise smtplib.SMTPRecipientsRefused({"nobody@example.com": (550, b"no such user")})

    p = make_product(pool, stock=10)
    place(pool, [(p["id"], 1)])
    assert drain(pool, RefusingBackend()) == 1
    row = dict(scalar_row(pool))
    assert row["status"] == "failed" and row["attempts"] == 1                 # no pointless retries
    [entry] = attempts(pool, row["id"])
    assert entry["permanent"] is True and "SMTPRecipientsRefused" in entry["error"]
    make_due(pool)
    assert drain(pool, FakeBackend()) == 0


def test_one_bad_notification_does_not_block_the_queue(pool):
    with pool.connection() as conn:                           # the OLDEST notification points at a missing report
        conn.execute("INSERT INTO notifications (type, payload, recipient) VALUES ('daily_report', %s, "
                     "'boss@example.com')", (Jsonb({"report_id": 999999}),))
    p = make_product(pool, stock=10)
    place(pool, [(p["id"], 1)])
    backend = FakeBackend()
    assert drain(pool, backend) == 2
    assert scalar(pool, "SELECT status FROM notifications WHERE id = 1") == "failed"
    assert scalar(pool, "SELECT status FROM notifications WHERE id = 2") == "sent"
    assert len(backend.sent) == 1                             # the real order email went out
    assert attempts(pool, 1)[0]["permanent"] is True


def test_manual_retry_after_the_problem_is_fixed(pool):
    p = make_product(pool, stock=10)
    place(pool, [(p["id"], 1)])
    for _ in range(worker.MAX_ATTEMPTS):
        make_due(pool)
        drain(pool, BrokenBackend())
    assert scalar(pool, "SELECT status FROM notifications") == "failed"

    with pool.connection() as conn:
        detail = ops.retry_notification(conn, 1)
    assert detail["status"] == "pending" and detail["attempts"] == 0 and len(detail["attempt_log"]) == 5
    backend = FakeBackend()
    assert drain(pool, backend) == 1 and len(backend.sent) == 1
    assert scalar(pool, "SELECT status FROM notifications") == "sent"
    assert len(attempts(pool, 1)) == 6                        # five failures and the success, all kept


def test_a_malformed_address_is_refused_before_contacting_the_mail_server(pool):
    with pool.connection() as conn:                  # e.g. data entered before validation existed
        conn.execute("INSERT INTO notifications (type, payload, recipient) VALUES ('low_stock', %s, %s)",
                     (Jsonb({"name": "W", "sku": "W-1", "stock": 1, "threshold": 5}), "oylad@8.io0_b!},n"))
    backend = FakeBackend()
    assert drain(pool, backend) == 1
    assert backend.sent == []                        # the mail server was never contacted
    row = dict(scalar_row(pool))
    assert row["status"] == "failed" and row["attempts"] == 1 and "Invalid recipient address" in row["last_error"]
    assert attempts(pool, row["id"])[0]["permanent"] is True


def test_dismissing_a_failed_notification_clears_it_but_keeps_the_history(pool):
    with pool.connection() as conn:
        conn.execute("INSERT INTO notifications (type, payload, recipient) VALUES ('low_stock', %s, 'bad address')",
                     (Jsonb({"name": "W", "sku": "W-1", "stock": 1, "threshold": 5}),))
    drain(pool, FakeBackend())
    with pool.connection() as conn:
        done = ops.dismiss_notification(conn, 1)
    assert done["status"] == "skipped" and "Dismissed manually" in done["last_error"]
    assert len(done["attempt_log"]) == 1                      # what went wrong is still on record
    with pool.connection() as conn:                           # ...but it no longer counts as a problem in reports
        now = datetime.now(timezone.utc)
        assert jobs.build_report(conn, now, now)["delivery"]["failed"] == 0


def wait_until(condition, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return False


def test_worker_loop_delivers_runs_jobs_reports_health_and_stops_cleanly(pool, db_url):
    backend, stop = FakeBackend(), threading.Event()
    thread = threading.Thread(target=worker.run_forever, daemon=True, kwargs=dict(
        url=db_url, stop=stop, interval=0.1, backend=backend, sender=SENDER,
        settings=jobs.Settings(recipients=("boss@example.com",))))
    thread.start()
    try:
        assert wait_until(lambda: scalar(pool, "SELECT count(*) FROM schedules") == 2)     # schedules set up itself
        p = make_product(pool, stock=10)
        place(pool, [(p["id"], 1)])
        assert wait_until(lambda: scalar(pool, "SELECT count(*) FROM notifications WHERE status = 'sent'") == 1)
        with pool.connection() as conn:
            conn.execute("UPDATE schedules SET next_run_at = now() WHERE name = 'daily_report'")
        assert wait_until(lambda: scalar(pool, "SELECT count(*) FROM reports") == 1)        # scheduled job ran by itself
        assert wait_until(lambda: len(backend.sent) == 2)                                   # ...and its email went out
        assert wait_until(lambda: scalar(pool, "SELECT count(*) FROM notifications WHERE status = 'pending'") == 0)
        with pool.connection() as conn:
            status = ops.ops_status(conn)
        assert status["worker"]["alive"] is True
        assert status["notifications"]["pending"] == 0
        assert {s["name"] for s in status["schedules"]} == {"daily_report", "housekeeping"}
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
