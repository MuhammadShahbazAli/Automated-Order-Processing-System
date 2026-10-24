"""Cross-cutting HTTP behaviour: request ids, access log, security headers, body-size limit, CORS."""
import logging
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from .logging_setup import current_context, request_ctx
from .settings import Settings

access_log = logging.getLogger("inventory.access")
log = logging.getLogger("inventory.http")

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


def error_response(status: int, code: str, message: str, details: list[dict] | None = None,
                   headers: dict | None = None) -> JSONResponse:
    """The one error shape used everywhere. `request_id` lets support find the matching log lines."""
    error = {"code": code, "message": message, "details": details or []}
    request_id = current_context().get("request_id")
    if request_id:
        error["request_id"] = request_id
    return JSONResponse(status_code=status, content={"error": error}, headers=headers)


def client_ip(request: Request, settings: Settings) -> str:
    if settings.trust_proxy_headers:                       # only behind a proxy you control: the header is spoofable
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


def install(app: FastAPI, settings: Settings) -> None:
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        supplied = request.headers.get("x-request-id", "")
        request_id = supplied if _REQUEST_ID.fullmatch(supplied) else uuid.uuid4().hex
        ctx = {"request_id": request_id, "user_id": None}
        token = request_ctx.set(ctx)
        started = time.perf_counter()
        response = None
        try:
            length = request.headers.get("content-length")
            if length is not None and not length.isdigit():
                response = error_response(400, "BAD_REQUEST", "Invalid Content-Length header.")
            elif length is not None and int(length) > settings.max_body_bytes:
                response = error_response(413, "PAYLOAD_TOO_LARGE",
                                          f"The request body may be at most {settings.max_body_bytes} bytes.")
            if response is None:
                try:
                    response = await call_next(request)
                except Exception:                          # nothing internal ever reaches the client
                    log.exception("Unhandled error", extra={"event": "unhandled_error", "path": request.url.path})
                    response = error_response(500, "INTERNAL_ERROR", "Something went wrong on our side.")

            response.headers["X-Request-ID"] = request_id
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            if request.url.path not in _DOC_PATHS and not request.url.path.startswith("/docs/"):
                response.headers["Cache-Control"] = "no-store"
                if response.headers.get("content-type", "").startswith("application/json"):
                    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
            if settings.hsts:
                response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            return response
        finally:
            status = response.status_code if response is not None else 500
            level = logging.DEBUG if request.url.path == "/health" else logging.INFO
            access_log.log(level, "request", extra={
                "event": "http_request", "method": request.method, "path": request.url.path, "status": status,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "client_ip": client_ip(request, settings), "user_id": ctx.get("user_id"),
            })
            request_ctx.reset(token)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
            expose_headers=["X-Request-ID", "Retry-After", "Idempotent-Replay"], max_age=600)
