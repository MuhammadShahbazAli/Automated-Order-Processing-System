"""Email delivery worker (needs TEST_DATABASE_URL). Uses a real mail-file backend or fakes; never SMTP."""
import threading
from email import policy
from email.parser import BytesParser

from app import services, worker
from app.emailer import FileBackend
from tests.conftest import make_product, place, scalar
from tests.test_concurrency import fire

SENDER = "shop@example.com"


class FakeBackend:
    def __init__(self):
        self.sent, self._lock = [], threading.Lock()

    def send(self, msg, name_hint):
        with self._lock:
            self.sent.append((name_hint, msg))
        return "fake"


class BrokenBackend:
    def send(self, msg, name_hint):
        raise RuntimeError("smtp down")


def drain(pool, backend):
    handled = 0
    with pool.connection() as conn:
        while worker.process_one(conn, backend, SENDER):
            handled += 1
    return handled


def make_due(pool):
    with pool.connection() as conn:
        conn.execute("UPDATE notifications SET next_attempt_at = now() - interval '1 second'")


def test_confirmation_email_with_invoice_is_delivered(pool, tmp_path):
    p = make_product(pool, stock=10)
    order = place(pool, [(p["id"], 2)])
    assert drain(pool, FileBackend(tmp_path)) == 1
    files = list(tmp_path.glob("*.eml"))
    assert len(files) == 1
    msg = BytesParser(policy=policy.default).parsebytes(files[0].read_bytes())
    assert msg["To"] == "ayesha@example.com" and msg["From"] == SENDER
    assert f"#{order['id']}" in msg["Subject"]
    attachments = list(msg.iter_attachments())
    assert [a.get_filename() for a in attachments] == [f"{order['invoice_number']}.pdf"]
    assert attachments[0].get_content().startswith(b"%PDF")
    assert scalar(pool, "SELECT status FROM notifications") == "sent"
    assert scalar(pool, "SELECT sent_at IS NOT NULL FROM notifications") is True
    assert drain(pool, FileBackend(tmp_path)) == 0               # nothing is sent twice


def test_every_lifecycle_step_emails_the_customer(pool):
    p = make_product(pool, stock=10)
    order = place(pool, [(p["id"], 1)])
    for step in ("processing", "shipped", "completed"):
        with pool.connection() as conn:
            services.transition_order(conn, order["id"], step)
    backend = FakeBackend()
    assert drain(pool, backend) == 3                             # processing is internal: no email
    subjects = [m["Subject"] for _, m in backend.sent]
    assert "confirmed" in subjects[0] and "shipped" in subjects[1] and "complete" in subjects[2]
    has_pdf = [bool(list(m.iter_attachments())) for _, m in backend.sent]
    assert has_pdf == [True, False, True]


def test_cancellation_email_is_sent_without_invoice(pool):
    p = make_product(pool, stock=10)
    order = place(pool, [(p["id"], 1)])
    with pool.connection() as conn:
        services.transition_order(conn, order["id"], "cancelled", "Out of budget")
    backend = FakeBackend()
    drain(pool, backend)
    cancelled = backend.sent[-1][1]
    assert "cancelled" in cancelled["Subject"] and "Out of budget" in cancelled.get_content()
    assert not list(cancelled.iter_attachments())


def test_failed_delivery_is_kept_and_retried_later(pool):
    p = make_product(pool, stock=10)
    place(pool, [(p["id"], 1)])
    assert drain(pool, BrokenBackend()) == 1
    row = dict(scalar_row(pool))
    assert row["status"] == "pending" and row["attempts"] == 1 and "smtp down" in row["last_error"]
    assert drain(pool, FakeBackend()) == 0                       # backoff: not due yet
    make_due(pool)
    backend = FakeBackend()
    assert drain(pool, backend) == 1 and len(backend.sent) == 1
    row = dict(scalar_row(pool))
    assert row["status"] == "sent" and row["attempts"] == 2


def test_worker_gives_up_after_max_attempts(pool):
    p = make_product(pool, stock=10)
    place(pool, [(p["id"], 1)])
    for _ in range(worker.MAX_ATTEMPTS):
        make_due(pool)
        drain(pool, BrokenBackend())
    row = dict(scalar_row(pool))
    assert row["status"] == "failed" and row["attempts"] == worker.MAX_ATTEMPTS


def test_notification_without_recipient_is_skipped(pool):
    p = make_product(pool, stock=10)
    with pool.connection() as conn:
        services.create_order(conn, "k", [(p["id"], 1)])         # internal order: no customer
    backend = FakeBackend()
    assert drain(pool, backend) == 1 and backend.sent == []
    assert scalar(pool, "SELECT status FROM notifications") == "skipped"


def test_low_stock_alert_goes_to_admin_email(pool, monkeypatch):
    monkeypatch.setattr(worker, "ADMIN_EMAIL", "boss@example.com")
    p = make_product(pool, stock=6, threshold=5)
    place(pool, [(p["id"], 2)])                                  # 6 -> 4 crosses the threshold
    backend = FakeBackend()
    drain(pool, backend)
    alerts = [m for _, m in backend.sent if m["To"] == "boss@example.com"]
    assert len(alerts) == 1 and "Low stock" in alerts[0]["Subject"]


def test_low_stock_without_admin_email_is_skipped(pool, monkeypatch):
    monkeypatch.setattr(worker, "ADMIN_EMAIL", "")
    p = make_product(pool, stock=6, threshold=5)
    place(pool, [(p["id"], 2)])
    drain(pool, FakeBackend())
    assert scalar(pool, "SELECT status FROM notifications WHERE type = 'low_stock'") == "skipped"


def test_several_workers_never_send_the_same_email_twice(pool):
    p = make_product(pool, stock=100)
    for _ in range(20):
        place(pool, [(p["id"], 1)])
    backend = FakeBackend()
    fire(pool, 4, lambda i: drain(pool, backend))
    names = [name for name, _ in backend.sent]
    assert len(names) == len(set(names))
    assert len([n for n in names if n.endswith("order_confirmed")]) == 20


def scalar_row(pool):
    with pool.connection() as conn:
        return conn.execute("SELECT * FROM notifications ORDER BY id LIMIT 1").fetchone()
