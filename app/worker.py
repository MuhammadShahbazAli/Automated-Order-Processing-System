"""Email delivery worker.

    python -m app.worker            # keep running, check for new notifications every 5 seconds
    python -m app.worker --once     # deliver everything that is due, then exit

The API only RECORDS notifications (inside the order's own transaction). This process delivers
them, so a slow or broken mail server can never slow down or fail an order.

Delivery is at-least-once: each notification is handled in its own transaction using
FOR UPDATE SKIP LOCKED, so several workers can run side by side without double-sending. If the
process dies after sending but before committing, that one email may be sent again.
"""
import argparse
import logging
import os
import time

import psycopg
from psycopg.rows import dict_row

from . import emailer, services
from .config import ADMIN_EMAIL, COMPANY, DEFAULT_DATABASE_URL
from .invoice import build_invoice_pdf

log = logging.getLogger("inventory_worker")

MAX_ATTEMPTS = 5
INVOICE_ATTACHED = {"order_confirmed", "order_completed"}


def _retry_delay_seconds(attempts: int) -> int:
    return min(30 * 2 ** attempts, 3600)      # 1 min, 2 min, 4 min, ... capped at 1 hour


def process_one(conn, backend, sender: str) -> bool:
    """Deliver the oldest due notification. Returns False when nothing was due."""
    with conn.transaction():
        n = conn.execute(
            "SELECT * FROM notifications WHERE status = 'pending' AND next_attempt_at <= now() "
            "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"
        ).fetchone()
        if n is None:
            return False

        recipient = n["recipient"] or (ADMIN_EMAIL if n["type"] == "low_stock" else "")
        if not recipient:
            conn.execute("UPDATE notifications SET status = 'skipped', last_error = %s WHERE id = %s",
                         ("No recipient (customer email missing or ADMIN_EMAIL not set)", n["id"]))
            return True

        order = None if n["type"] == "low_stock" else services.get_order(conn, n["payload"]["order_id"])
        try:
            pdf = build_invoice_pdf(order, COMPANY) if n["type"] in INVOICE_ATTACHED else None
            msg = emailer.build_message(
                n["type"], recipient=recipient, sender=sender, company_name=COMPANY["name"],
                currency=COMPANY["currency"], order=order, payload=n["payload"], invoice_pdf=pdf)
            where = backend.send(msg, f"n{n['id']}-{n['type']}")
        except Exception as exc:                      # mail problems must never lose the notification
            attempts = n["attempts"] + 1
            status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
            log.warning("Notification %s failed (attempt %s/%s): %s", n["id"], attempts, MAX_ATTEMPTS, exc)
            conn.execute(
                "UPDATE notifications SET status = %s, attempts = %s, last_error = %s, "
                "next_attempt_at = now() + make_interval(secs => %s) WHERE id = %s",
                (status, attempts, str(exc)[:500], _retry_delay_seconds(attempts), n["id"]))
        else:
            log.info("Notification %s (%s) sent to %s via %s", n["id"], n["type"], recipient, where)
            conn.execute("UPDATE notifications SET status = 'sent', attempts = attempts + 1, sent_at = now(), "
                         "last_error = NULL WHERE id = %s", (n["id"],))
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Deliver pending notification emails.")
    parser.add_argument("--once", action="store_true", help="deliver what is due, then exit")
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between checks when idle")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    backend, sender = emailer.backend_from_env(), emailer.sender_from_env()
    url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    conn, handled_total = None, 0
    log.info("Worker started (backend: %s)", type(backend).__name__)
    while True:
        try:
            if conn is None or conn.closed:
                conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
            if process_one(conn, backend, sender):
                handled_total += 1
                continue
        except psycopg.OperationalError as exc:
            log.error("Database problem, will retry: %s", exc)
            conn = None
            if args.once:
                raise SystemExit(1)
        if args.once:
            break
        time.sleep(args.interval)
    log.info("Done: %s notification(s) processed", handled_total)


if __name__ == "__main__":
    main()
