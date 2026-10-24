"""Schedule arithmetic (standard library only)."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def get_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RuntimeError(
            f"Time zone '{name}' not found. Use a name like 'Asia/Karachi'. "
            "On Windows also run: pip install tzdata") from exc


def compute_next_run(after: datetime, run_at: time, tz: ZoneInfo) -> datetime:
    """The first moment strictly after `after` when the local clock in `tz` shows `run_at` (returned in UTC)."""
    local = after.astimezone(tz)
    candidate = datetime.combine(local.date(), run_at, tzinfo=tz)
    if candidate <= local:
        candidate = datetime.combine(local.date() + timedelta(days=1), run_at, tzinfo=tz)
    return candidate.astimezone(timezone.utc)
