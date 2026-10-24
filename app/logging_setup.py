"""Structured logging: one JSON object per line, with a request id on every line."""
import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

# Set per request by the HTTP middleware: {"request_id": ..., "user_id": ...}. A mutable dict, so code that
# runs later in the request (e.g. authentication) can fill in the user id and the middleware still sees it.
request_ctx: ContextVar = ContextVar("request_ctx", default=None)

_SECRET_WORDS = ("password", "passwd", "token", "authorization", "secret", "api_key", "apikey", "cookie")


def current_context() -> dict:
    return request_ctx.get() or {}


def redact_secrets(value):
    """Safety net: anything that looks like a secret is replaced by '***' before it reaches a log."""
    if isinstance(value, dict):
        return {k: ("***" if any(w in str(k).lower() for w in _SECRET_WORDS) else redact_secrets(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_secrets(v) for v in value]
    return value


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        ctx = current_context()
        if not hasattr(record, "request_id"):
            record.request_id = ctx.get("request_id") or "-"
        if not hasattr(record, "user_id"):
            record.user_id = ctx.get("user_id")
        return True


_STANDARD = set(vars(logging.LogRecord("x", 0, "x", 0, "x", None, None))) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(redact_secrets(payload), default=str, ensure_ascii=False)


def setup_logging(fmt: str = "text", level: str = "INFO") -> None:
    """Idempotent. Replaces only the handler this function installed earlier (never foreign handlers)."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_inventory_handler", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler._inventory_handler = True
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter() if fmt == "json" else logging.Formatter(
        "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for name in ("uvicorn", "uvicorn.error"):             # route uvicorn's own messages through our handler
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True   # we write one structured access line per request
