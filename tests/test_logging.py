"""Structured logging. No database needed."""
import json
import logging

from app.logging_setup import JsonFormatter, redact_secrets, request_ctx


def record(message="hello", level=logging.INFO, **extra):
    r = logging.LogRecord("inventory.test", level, __file__, 1, message, None, None)
    for key, value in extra.items():
        setattr(r, key, value)
    return r


def test_every_line_is_valid_json_with_the_basics():
    line = JsonFormatter().format(record("order placed", order_id=42, status="confirmed"))
    data = json.loads(line)
    assert data["message"] == "order placed" and data["level"] == "INFO" and data["logger"] == "inventory.test"
    assert data["order_id"] == 42 and data["ts"].endswith("+00:00")
    assert "\n" not in line


def test_exceptions_are_included_without_breaking_the_line_format():
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys
        r = record("failed", logging.ERROR)
        r.exc_info = sys.exc_info()
    line = JsonFormatter().format(r)
    assert "\n" not in line and "RuntimeError: boom" in json.loads(line)["exception"]


def test_secrets_never_reach_the_log():
    line = JsonFormatter().format(record("login", password="hunter2", authorization="Bearer inv_abc",
                                         headers={"Cookie": "s=1", "Accept": "x"}, access_token="inv_zzz"))
    for leaked in ("hunter2", "inv_abc", "inv_zzz", "s=1"):
        assert leaked not in line
    assert json.loads(line)["headers"]["Accept"] == "x"


def test_redact_secrets_is_recursive():
    out = redact_secrets({"user": {"Password": "p", "name": "a"}, "items": [{"token": "t", "n": 1}]})
    assert out == {"user": {"Password": "***", "name": "a"}, "items": [{"token": "***", "n": 1}]}


def test_unserialisable_values_do_not_crash_logging():
    data = json.loads(JsonFormatter().format(record("x", thing=object())))
    assert "object" in data["thing"]


def test_request_context_is_available_to_the_filter():
    from app.logging_setup import ContextFilter
    token = request_ctx.set({"request_id": "abc123", "user_id": 7})
    try:
        r = record("x")
        ContextFilter().filter(r)
        data = json.loads(JsonFormatter().format(r))
    finally:
        request_ctx.reset(token)
    assert data["request_id"] == "abc123" and data["user_id"] == 7
    r = record("outside a request")
    ContextFilter().filter(r)
    assert json.loads(JsonFormatter().format(r))["request_id"] == "-"
