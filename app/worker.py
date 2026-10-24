"""Background worker: delivers emails, runs scheduled jobs, reports its own health.

    python -m app.worker            # run forever
    python -m app.worker --once     # do everything that is due, then exit

By default the API starts this same loop in a background thread (RUN_WORKER_IN_API=1), so a single
`uvicorn app.main:app` is enough. You can also run it as a separate process; several workers at once are safe.

Guarantees
  * The API never sends email. It only writes notification rows inside the order's own transaction, so an
    email problem can never undo or delay an accepted order.
  * Every delivery attempt is recorded (notification_attempts). Temporary failures retry with backoff
    (1, 2, 4, 8, 16 min); after MAX_ATTEMPTS, or at once for errors retrying cannot fix (bad address,
    missing data), the notification is marked 'failed'. A failed one never blocks the ones behind it.
  * Each notification is handled in its own transaction with FOR UPDATE SKIP LOCKED: two workers never send
    the same one. If a worker dies mid-send, nothing is lost: the row simply becomes due again
    (at-least-once delivery, so in a rare crash window one email may arrive twice).
"""
import argparse
import logging
import os
import smtplib
import socket
import threading

import psycopg
from psycopg.rows import dict_row

from . import emailer, jobs, services
from .config import ADMIN_EMAIL, COMPANY, DEFAULT_DATABASE_URL
from .errors import AppError
from .invoice import build_invoice_pdf
from .logging_setup import setup_logging
from .settings import Settings
from .timeutil import get_zone
from .validation import is_valid_email

log = logging.getLogger("inventory_worker")

MAX_ATTEMPTS = 5
INVOICE_ATTACHED = {"order_confirmed", "order_completed"}
# Retrying cannot fix these: give up immediately instead of hammering the mail server.
PERMANENT_ERRORS = (smtplib.SMTPRecipientsRefused, ValueError, LookupError, TypeError, AssertionError, AppError)


def _retry_delay_seconds(attempts: int) -> int:
    return min(30 * 2 ** attempts, 3600)      # 1 min, 2 min, 4 min, ... capped at 1 hour


def _load(conn, n: dict):
    """Fetch what the email is about. Returns (order, report)."""
    if n["type"] == "low_stock":
        return None, None
    if n["type"] == "daily_report":
        row = conn.execute("SELECT data FROM reports WHERE id = %s", (n["payload"]["report_id"],)).fetchone()
        if row is None:
            raise LookupError(f"Report {n['payload']['report_id']} not found")
        return None, row["data"]
    return services.get_order(conn, n["payload"]["order_id"]), None


def _log_attempt(conn, notification_id: int, attempt: int, outcome: str, permanent: bool, error: str | None) -> None:
    conn.execute("INSERT INTO notification_attempts (notification_id, attempt, outcome, permanent, error) "
                 "VALUES (%s, %s, %s, %s, %s)", (notification_id, attempt, outcome, permanent, error))


def process_one(conn, backend, sender: str, tz=None) -> bool:
    """Deliver the oldest due notification. Returns False when nothing was due."""
    with conn.transaction():
        n = conn.execute(
            "SELECT * FROM notifications WHERE status = 'pending' AND next_attempt_at <= now() "
            "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"
        ).fetchone()
        if n is None:
            return False

        attempt = n["attempts"] + 1
        recipient = n["recipient"] or (ADMIN_EMAIL if n["type"] == "low_stock" else "")
        if not recipient:
            reason = "No recipient (customer email missing, or ADMIN_EMAIL / REPORT_RECIPIENTS not set)"
            conn.execute("UPDATE notifications SET status = 'skipped', last_error = %s WHERE id = %s",
                         (reason, n["id"]))
            _log_attempt(conn, n["id"], attempt, "skipped", False, reason)
            return True

        try:
            if not is_valid_email(recipient):  # checked BEFORE contacting the mail server (protects its reputation)
                raise ValueError(f"Invalid recipient address {recipient!r}; nothing was sent")
            with conn.transaction():          # savepoint: a failed read must not poison the outer transaction
                order, report = _load(conn, n)
            pdf = build_invoice_pdf(order, COMPANY) if n["type"] in INVOICE_ATTACHED else None
            msg = emailer.build_message(
                n["type"], recipient=recipient, sender=sender, company_name=COMPANY["name"],
                currency=COMPANY["currency"], order=order, payload=n["payload"], invoice_pdf=pdf,
                report=report, tz=tz)
            where = backend.send(msg, f"n{n['id']}-{n['type']}")
        except Exception as exc:              # mail problems must never lose the notification or stop the worker
            permanent = isinstance(exc, PERMANENT_ERRORS)
            error = f"{type(exc).__name__}: {exc}"[:500]
            give_up = permanent or attempt >= MAX_ATTEMPTS
            log.warning("Notification %s failed (attempt %s/%s%s): %s", n["id"], attempt, MAX_ATTEMPTS,
                        ", permanent" if permanent else "", error,
                        extra={"event": "notification_failed", "notification_id": n["id"],
                               "notification_type": n["type"], "attempt": attempt, "permanent": permanent})
            conn.execute(
                "UPDATE notifications SET status = %s, attempts = %s, last_error = %s, "
                "next_attempt_at = now() + make_interval(secs => %s) WHERE id = %s",
                ("failed" if give_up else "pending", attempt, error, _retry_delay_seconds(attempt), n["id"]))
            _log_attempt(conn, n["id"], attempt, "failed", permanent, error)
        else:
            log.info("Notification %s (%s) sent via %s", n["id"], n["type"], where,
                     extra={"event": "notification_sent", "notification_id": n["id"], "notification_type": n["type"]})
            conn.execute("UPDATE notifications SET status = 'sent', attempts = %s, sent_at = now(), "
                         "last_error = NULL WHERE id = %s", (attempt, n["id"]))
            _log_attempt(conn, n["id"], attempt, "sent", False, None)
        return True


def heartbeat(conn, worker_id: str) -> None:
    conn.execute(
        "INSERT INTO worker_heartbeats (worker_id, info) VALUES (%s, %s) "
        "ON CONFLICT (worker_id) DO UPDATE SET last_seen = now(), info = EXCLUDED.info",
        (worker_id, f"pid {os.getpid()}"))


def tick(conn, backend, sender: str, settings: jobs.Settings, worker_id: str, batch: int = 25) -> int:
    """One pass: heartbeat, due scheduled jobs, then up to `batch` due emails. Returns how much work was done."""
    heartbeat(conn, worker_id)
    work = 0
    while jobs.run_one_schedule(conn, settings):
        work += 1
    tz = get_zone(settings.timezone)
    for _ in range(batch):
        if not process_one(conn, backend, sender, tz):
            break
        work += 1
    return work


def run_forever(url: str, stop: threading.Event, interval: float = 5.0, once: bool = False,
                backend=None, sender: str | None = None, settings: jobs.Settings | None = None) -> int:
    """The worker loop. Never dies from a failing job or a database hiccup; stops when `stop` is set."""
    backend = backend or emailer.backend_from_env()
    sender = sender or emailer.sender_from_env()
    settings = settings or jobs.Settings.from_env()
    settings.validate()
    worker_id = f"{socket.gethostname()}-{os.getpid()}-{threading.current_thread().name}"
    conn, handled = None, 0
    log.info("Worker %s started (email backend: %s, reports at %s %s)", worker_id, type(backend).__name__,
             settings.report_time.strftime("%H:%M"), settings.timezone)
    while not stop.is_set():
        try:
            if conn is None or conn.closed:
                conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
                jobs.ensure_schedules(conn, settings)
            done = tick(conn, backend, sender, settings, worker_id)
            handled += done
            if done:
                continue
        except psycopg.OperationalError as exc:
            log.error("Database problem, will reconnect: %s", exc)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            conn = None
            if once:
                raise SystemExit(1)
        except Exception:
            log.exception("Worker loop error (the worker keeps running)")
            if once:
                raise
        if once:
            break
        stop.wait(interval)
    if conn is not None and not conn.closed:
        conn.close()
    log.info("Worker %s stopped after %s item(s)", worker_id, handled)
    return handled


def main() -> None:
    parser = argparse.ArgumentParser(description="Inventory background worker.")
    parser.add_argument("--once", action="store_true", help="do what is due, then exit")
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between checks when idle")
    args = parser.parse_args()
    app_settings = Settings.from_env()
    setup_logging(app_settings.log_format, app_settings.log_level)
    run_forever(os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL), threading.Event(),
                interval=args.interval, once=args.once)


if __name__ == "__main__":
    main()
