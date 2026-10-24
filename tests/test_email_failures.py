"""Email trouble must never touch orders, and must always leave a record (needs TEST_DATABASE_URL)."""
import smtplib
import threading
import time

from app import emailer, jobs, ops, worker
from tests.conftest import make_product, place, scalar
from tests.test_api import CUSTOMER
from tests.test_api import place as place_via_api
from tests.test_worker import FakeBackend, SENDER, drain, make_due

CLOSED_PORT_BACKEND = lambda: emailer.SmtpBackend("127.0.0.1", 1, "", "", False)   # nothing listens on port 1


def last_error(pool):
    return scalar(pool, "SELECT last_error FROM notifications ORDER BY id LIMIT 1")


def test_a_mail_server_that_refuses_connections_is_retried_not_fatal(pool):
    p = make_product(pool, stock=10)
    place(pool, [(p["id"], 1)])
    assert drain(pool, CLOSED_PORT_BACKEND()) == 1                      # a REAL refused connection, no mocking
    assert scalar(pool, "SELECT status FROM notifications") == "pending"
    assert scalar(pool, "SELECT attempts FROM notifications") == 1
    assert "ConnectionRefusedError" in last_error(pool) or "Error" in last_error(pool)
    with pool.connection() as conn:
        log = ops.get_notification(conn, 1)["attempt_log"]
    assert len(log) == 1 and log[0]["outcome"] == "failed" and log[0]["permanent"] is False


def test_rejected_credentials_are_retried_and_recorded(pool, monkeypatch):
    class WrongPasswordSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            pass

        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    monkeypatch.setattr(emailer.smtplib, "SMTP", WrongPasswordSMTP)
    p = make_product(pool, stock=10)
    place(pool, [(p["id"], 1)])
    assert drain(pool, emailer.SmtpBackend("smtp.example.com", 587, "me@example.com", "stale-password", True)) == 1
    assert scalar(pool, "SELECT status FROM notifications") == "pending"           # an admin can fix the password
    assert "SMTPAuthenticationError" in last_error(pool) and "stale-password" not in last_error(pool)


def test_orders_keep_working_while_the_mail_worker_is_stuck_mid_send(web, pool, db_url):
    """A hung mail server freezes the worker inside send(); customers must not notice."""
    staff = web.client("staff")
    p = make_product(pool, stock=50)
    place(pool, [(p["id"], 1)])                                         # first notification: the worker will hang on it

    entered, release = threading.Event(), threading.Event()

    class HangingBackend:
        def send(self, msg, name_hint):
            entered.set()
            release.wait(timeout=30)                                    # "the mail server never answers"
            return "late"

    stop = threading.Event()
    thread = threading.Thread(target=worker.run_forever, daemon=True, kwargs=dict(
        url=db_url, stop=stop, interval=0.1, backend=HangingBackend(), sender=SENDER,
        settings=jobs.Settings()))
    thread.start()
    try:
        assert entered.wait(timeout=10), "the worker never started sending"
        started = time.time()
        for i in range(5):                                              # five more orders while it is stuck
            assert place_via_api(staff, [{"product_id": p["id"], "quantity": 1}], f"during-hang-{i}").status_code == 201
        assert staff.patch(f"/products/{p['id']}/stock", json={"adjust_by": 5}).status_code == 200
        assert time.time() - started < 5                                # nobody waited for the mail server
        assert scalar(pool, "SELECT count(*) FROM orders") == 6
    finally:
        release.set()
        stop.set()
        thread.join(timeout=15)
    assert not thread.is_alive()


def test_the_dashboard_and_report_show_that_email_is_failing(web, pool):
    admin = web.client("admin")
    p = make_product(pool, stock=10)
    place(pool, [(p["id"], 1)])
    for _ in range(worker.MAX_ATTEMPTS):
        make_due(pool)
        drain(pool, CLOSED_PORT_BACKEND())
    status = admin.get("/ops/status").json()["notifications"]
    assert status["failed"] == 1 and status["pending"] == 0
    failed = admin.get("/notifications", params={"status": "failed"}).json()["items"]
    assert len(failed) == 1 and failed[0]["attempts"] == worker.MAX_ATTEMPTS

    with pool.connection() as conn:
        jobs.ensure_schedules(conn, jobs.Settings(recipients=("boss@example.com",)))
    admin.post("/ops/schedules/daily_report/run-now")
    with pool.connection() as conn:
        jobs.run_one_schedule(conn, jobs.Settings(recipients=("boss@example.com",)))
    report = admin.get("/reports").json()["items"][0]["data"]
    assert report["delivery"]["failed"] == 1
    subject, body = emailer.render_report(report, "Shop", "PKR")
    assert subject.startswith("[ACTION NEEDED]") and "failed permanently" in body


def test_once_the_mail_server_is_back_a_manual_retry_delivers_everything(pool):
    p = make_product(pool, stock=10)
    place(pool, [(p["id"], 1)])
    for _ in range(worker.MAX_ATTEMPTS):
        make_due(pool)
        drain(pool, CLOSED_PORT_BACKEND())
    assert scalar(pool, "SELECT status FROM notifications") == "failed"
    with pool.connection() as conn:
        ops.retry_notification(conn, 1)
    backend = FakeBackend()
    assert drain(pool, backend) == 1 and len(backend.sent) == 1
    assert scalar(pool, "SELECT status FROM notifications") == "sent"
