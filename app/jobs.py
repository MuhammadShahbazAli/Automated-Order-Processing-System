"""Scheduled jobs: daily report and housekeeping.

How a run works (all inside ONE database transaction):
  1. lock the due schedule row (FOR UPDATE SKIP LOCKED: other workers skip it)
  2. run the job inside a savepoint
  3a. success -> report + email notifications are saved AND next_run_at moves to the next slot, together
  3b. failure -> the job's partial writes are rolled back, the failure is recorded, and the job is
      retried soon (1 min, 2 min, 4 min ... capped at 1 hour) instead of waiting until tomorrow
If the worker was down for days, ONE catch-up run happens; the report simply covers everything since the
previous report (period = previous report's end -> now), so nothing is missed and nothing is counted twice.
"""
import logging
import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from psycopg.types.json import Jsonb

from . import services
from .config import ADMIN_EMAIL
from .timeutil import compute_next_run, get_zone

log = logging.getLogger("inventory_jobs")


@dataclass(frozen=True)
class Settings:
    report_time: time = time(8, 0)
    housekeeping_time: time = time(3, 0)
    timezone: str = "Asia/Karachi"
    recipients: tuple = ()
    idempotency_days: int = 30
    notification_days: int = 90
    security_event_retention_days: int = 180

    @classmethod
    def from_env(cls) -> "Settings":
        raw = os.environ.get("REPORT_RECIPIENTS", "") or ADMIN_EMAIL
        return cls(
            report_time=time.fromisoformat(os.environ.get("REPORT_TIME", "08:00")),
            housekeeping_time=time.fromisoformat(os.environ.get("HOUSEKEEPING_TIME", "03:00")),
            timezone=os.environ.get("REPORT_TIMEZONE", "Asia/Karachi"),
            recipients=tuple(p.strip() for p in raw.split(",") if p.strip()),
            idempotency_days=int(os.environ.get("IDEMPOTENCY_RETENTION_DAYS", "30")),
            notification_days=int(os.environ.get("NOTIFICATION_RETENTION_DAYS", "90")),
            security_event_retention_days=int(os.environ.get("SECURITY_EVENT_RETENTION_DAYS", "180")),
        )

    def validate(self) -> None:
        get_zone(self.timezone)


# ---------------------------------------------------------------- schedules
def ensure_schedules(conn, settings: Settings) -> None:
    """Create/update the built-in schedules so they match the settings. Safe to call repeatedly."""
    tz = get_zone(settings.timezone)
    now = datetime.now(timezone.utc)
    for name, job_type, run_at in (("daily_report", "daily_report", settings.report_time),
                                   ("housekeeping", "housekeeping", settings.housekeeping_time)):
        next_run = compute_next_run(now, run_at, tz)
        conn.execute(
            "INSERT INTO schedules (name, job_type, run_at, timezone, next_run_at) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (name) DO NOTHING", (name, job_type, run_at, settings.timezone, next_run))
        existing = conn.execute("SELECT id, run_at, timezone FROM schedules WHERE name = %s", (name,)).fetchone()
        if existing["run_at"] != run_at or existing["timezone"] != settings.timezone:
            conn.execute("UPDATE schedules SET run_at = %s, timezone = %s, next_run_at = %s WHERE id = %s",
                         (run_at, settings.timezone, next_run, existing["id"]))


def run_one_schedule(conn, settings: Settings) -> bool:
    """Run the oldest due schedule, if any. Returns False when nothing was due."""
    with conn.transaction():
        s = conn.execute(
            "SELECT *, now() AS db_now FROM schedules WHERE enabled AND next_run_at <= now() "
            "ORDER BY next_run_at LIMIT 1 FOR UPDATE SKIP LOCKED").fetchone()
        if s is None:
            return False
        now, scheduled_for = s["db_now"], s["next_run_at"]
        try:
            with conn.transaction():                       # savepoint: a failing job leaves nothing behind
                detail = HANDLERS[s["job_type"]](conn, now, settings)
        except Exception as exc:
            failures = s["consecutive_failures"] + 1
            retry_at = now + timedelta(seconds=min(60 * 2 ** failures, 3600))
            error = f"{type(exc).__name__}: {exc}"[:500]
            log.exception("Schedule '%s' failed (%s in a row); retrying at %s", s["name"], failures, retry_at)
            conn.execute(
                "UPDATE schedules SET next_run_at = %s, last_run_at = %s, last_status = 'failed', "
                "last_error = %s, consecutive_failures = %s WHERE id = %s",
                (retry_at, now, error, failures, s["id"]))
            conn.execute("INSERT INTO schedule_runs (schedule_name, scheduled_for, status, error) "
                         "VALUES (%s, %s, 'failed', %s)", (s["name"], scheduled_for, error))
        else:
            next_run = compute_next_run(max(now, scheduled_for), s["run_at"], get_zone(s["timezone"]))
            log.info("Schedule '%s' ok: %s", s["name"], detail)
            conn.execute(
                "UPDATE schedules SET next_run_at = %s, last_run_at = %s, last_status = 'ok', "
                "last_error = NULL, consecutive_failures = 0 WHERE id = %s", (next_run, now, s["id"]))
            conn.execute("INSERT INTO schedule_runs (schedule_name, scheduled_for, status, detail) "
                         "VALUES (%s, %s, 'ok', %s)", (s["name"], scheduled_for, detail))
        return True


# ---------------------------------------------------------------- daily report
def build_report(conn, start: datetime, end: datetime) -> dict:
    by_status_rows = conn.execute(
        "SELECT status, count(*) AS n, COALESCE(sum(total_cents), 0)::bigint AS cents FROM orders "
        "WHERE created_at >= %s AND created_at < %s GROUP BY status", (start, end)).fetchall()
    cancelled_in_period = conn.execute(
        "SELECT count(*) AS n FROM orders WHERE cancelled_at >= %s AND cancelled_at < %s",
        (start, end)).fetchone()["n"]
    top = conn.execute(
        "SELECT oi.sku, oi.name, SUM(oi.quantity)::bigint AS quantity, SUM(oi.line_total_cents)::bigint AS cents "
        "FROM order_items oi JOIN orders o ON o.id = oi.order_id "
        "WHERE o.created_at >= %s AND o.created_at < %s AND o.status <> 'cancelled' "
        "GROUP BY oi.product_id, oi.sku, oi.name ORDER BY quantity DESC, cents DESC, oi.sku LIMIT 5",
        (start, end)).fetchall()
    stock = conn.execute(
        "SELECT count(*) AS products, count(*) FILTER (WHERE stock = 0) AS out_of_stock, "
        "count(*) FILTER (WHERE stock > 0 AND stock <= low_stock_threshold) AS low_stock FROM products").fetchone()
    low_items = conn.execute(
        "SELECT id, sku, name, stock, low_stock_threshold FROM products "
        "WHERE stock <= low_stock_threshold ORDER BY stock, id LIMIT 25").fetchall()
    delivery = conn.execute(
        "SELECT count(*) FILTER (WHERE status = 'pending') AS pending, "
        "count(*) FILTER (WHERE status = 'failed') AS failed, "
        "count(*) FILTER (WHERE status = 'skipped') AS skipped, "
        "EXTRACT(EPOCH FROM (now() - min(created_at) FILTER (WHERE status = 'pending')))::bigint "
        "AS oldest_pending_seconds FROM notifications").fetchone()
    audit = services.audit_inventory(conn)

    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "orders": {
            "placed": sum(r["n"] for r in by_status_rows),
            "by_status": {r["status"]: r["n"] for r in by_status_rows},
            "net_revenue": services.fmt_money(sum(r["cents"] for r in by_status_rows if r["status"] != "cancelled")),
            "cancelled_in_period": cancelled_in_period,
        },
        "top_products": [{"sku": r["sku"], "name": r["name"], "quantity": r["quantity"],
                          "revenue": services.fmt_money(r["cents"])} for r in top],
        "stock": {"products": stock["products"], "out_of_stock": stock["out_of_stock"],
                  "low_stock": stock["low_stock"], "low_stock_items": low_items},
        "audit": {"consistent": audit["consistent"], "discrepancies": audit["discrepancies"][:10]},
        "delivery": {"pending": delivery["pending"], "failed": delivery["failed"],
                     "skipped": delivery["skipped"], "oldest_pending_seconds": delivery["oldest_pending_seconds"]},
    }


def run_daily_report(conn, now: datetime, settings: Settings) -> str:
    """Report on everything since the previous report (first ever: the last 24 hours)."""
    previous_end = conn.execute("SELECT max(period_end) AS e FROM reports WHERE kind = 'daily_report'").fetchone()["e"]
    start = previous_end if previous_end and previous_end < now else now - timedelta(days=1)
    data = build_report(conn, start, now)
    report_id = conn.execute(
        "INSERT INTO reports (kind, period_start, period_end, data) VALUES ('daily_report', %s, %s, %s) RETURNING id",
        (start, now, Jsonb(data))).fetchone()["id"]
    for recipient in settings.recipients:
        services.notify(conn, "daily_report", {"report_id": report_id}, recipient)
    return f"report {report_id} saved; queued for {len(settings.recipients)} recipient(s)"


# ---------------------------------------------------------------- housekeeping
def run_housekeeping(conn, now: datetime, settings: Settings) -> str:
    keys = conn.execute("DELETE FROM idempotency_keys WHERE created_at < now() - make_interval(days => %s)",
                        (settings.idempotency_days,)).rowcount
    notes = conn.execute(
        "DELETE FROM notifications WHERE status IN ('sent', 'skipped') "
        "AND created_at < now() - make_interval(days => %s)", (settings.notification_days,)).rowcount
    beats = conn.execute("DELETE FROM worker_heartbeats WHERE last_seen < now() - interval '1 day'").rowcount
    runs = conn.execute("DELETE FROM schedule_runs WHERE created_at < now() - interval '90 days'").rowcount
    sessions = conn.execute("DELETE FROM sessions WHERE expires_at < now() - interval '7 days' "
                            "OR revoked_at < now() - interval '7 days'").rowcount
    events = conn.execute("DELETE FROM security_events WHERE created_at < now() - make_interval(days => %s)",
                          (settings.security_event_retention_days,)).rowcount
    return (f"removed {keys} old idempotency key(s), {notes} old notification(s), "
            f"{beats} stale heartbeat(s), {runs} old run record(s), {sessions} dead session(s), "
            f"{events} old security event(s)")


HANDLERS = {"daily_report": run_daily_report, "housekeeping": run_housekeeping}
