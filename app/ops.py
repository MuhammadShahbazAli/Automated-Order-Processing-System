"""Operations: system status, stored reports, notification inspection and manual retry."""
from .errors import AppError

WORKER_STALE_SECONDS = 60


def ops_status(conn) -> dict:
    beats = conn.execute(
        "SELECT worker_id, info, started_at, last_seen, EXTRACT(EPOCH FROM (now() - last_seen))::bigint "
        "AS seconds_ago FROM worker_heartbeats ORDER BY last_seen DESC LIMIT 20").fetchall()
    counts = {"pending": 0, "sent": 0, "failed": 0, "skipped": 0}
    for row in conn.execute("SELECT status, count(*) AS n FROM notifications GROUP BY status").fetchall():
        counts[row["status"]] = row["n"]
    backlog = conn.execute(
        "SELECT EXTRACT(EPOCH FROM (now() - min(created_at)))::bigint AS s FROM notifications "
        "WHERE status = 'pending' AND next_attempt_at <= now()").fetchone()["s"]
    schedules = conn.execute(
        "SELECT name, job_type, run_at, timezone, enabled, next_run_at, last_run_at, last_status, last_error, "
        "consecutive_failures FROM schedules ORDER BY id").fetchall()
    for s in schedules:
        s["run_at"] = s["run_at"].strftime("%H:%M")
    runs = conn.execute(
        "SELECT schedule_name, scheduled_for, status, detail, error, created_at FROM schedule_runs "
        "ORDER BY id DESC LIMIT 10").fetchall()
    return {
        "worker": {"alive": any(b["seconds_ago"] <= WORKER_STALE_SECONDS for b in beats), "workers": beats},
        "notifications": {**counts, "oldest_due_seconds": backlog},
        "schedules": schedules,
        "recent_runs": runs,
    }


def run_schedule_now(conn, name: str) -> dict:
    """Make a schedule due immediately; the worker picks it up within seconds."""
    row = conn.execute("UPDATE schedules SET next_run_at = now() WHERE name = %s RETURNING name, next_run_at",
                       (name,)).fetchone()
    if row is None:
        raise AppError(404, "SCHEDULE_NOT_FOUND", f"No schedule named '{name}'.",
                       [{"field": "name", "issue": "Unknown schedule"}])
    return row


def list_reports(conn, limit: int) -> dict:
    rows = conn.execute("SELECT id, kind, period_start, period_end, created_at, data FROM reports "
                        "ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
    return {"items": rows}


def get_report(conn, report_id: int) -> dict:
    row = conn.execute("SELECT id, kind, period_start, period_end, created_at, data FROM reports WHERE id = %s",
                       (report_id,)).fetchone()
    if row is None:
        raise AppError(404, "REPORT_NOT_FOUND", f"Report {report_id} does not exist.",
                       [{"field": "report_id", "issue": "Report not found"}])
    return row


def get_notification(conn, notification_id: int) -> dict:
    n = conn.execute("SELECT * FROM notifications WHERE id = %s", (notification_id,)).fetchone()
    if n is None:
        raise AppError(404, "NOTIFICATION_NOT_FOUND", f"Notification {notification_id} does not exist.",
                       [{"field": "notification_id", "issue": "Notification not found"}])
    n["attempt_log"] = conn.execute(
        "SELECT attempt, outcome, permanent, error, created_at FROM notification_attempts "
        "WHERE notification_id = %s ORDER BY id", (notification_id,)).fetchall()
    return n


def retry_notification(conn, notification_id: int) -> dict:
    """Put a permanently failed notification back in the queue (e.g. after fixing the SMTP password)."""
    with conn.transaction():
        n = conn.execute("SELECT id, status FROM notifications WHERE id = %s FOR UPDATE",
                         (notification_id,)).fetchone()
        if n is None:
            raise AppError(404, "NOTIFICATION_NOT_FOUND", f"Notification {notification_id} does not exist.",
                           [{"field": "notification_id", "issue": "Notification not found"}])
        if n["status"] != "failed":
            raise AppError(409, "NOT_RETRYABLE", f"Only failed notifications can be retried (this one is '{n['status']}').",
                           [{"field": "status", "current": n["status"]}])
        conn.execute("UPDATE notifications SET status = 'pending', attempts = 0, last_error = NULL, "
                     "next_attempt_at = now() WHERE id = %s", (notification_id,))
    return get_notification(conn, notification_id)


def dismiss_notification(conn, notification_id: int) -> dict:
    """Clear a failed notification that can never succeed (e.g. the customer gave a bad address).
    It stops counting as a problem in reports; the error and attempt history are kept."""
    with conn.transaction():
        n = conn.execute("SELECT id, status, last_error FROM notifications WHERE id = %s FOR UPDATE",
                         (notification_id,)).fetchone()
        if n is None:
            raise AppError(404, "NOTIFICATION_NOT_FOUND", f"Notification {notification_id} does not exist.",
                           [{"field": "notification_id", "issue": "Notification not found"}])
        if n["status"] != "failed":
            raise AppError(409, "NOT_DISMISSABLE", f"Only failed notifications can be dismissed (this one is '{n['status']}').",
                           [{"field": "status", "current": n["status"]}])
        conn.execute("UPDATE notifications SET status = 'skipped', last_error = %s WHERE id = %s",
                     (f"Dismissed manually (was: {n['last_error']})"[:500], notification_id))
    return get_notification(conn, notification_id)
