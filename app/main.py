"""HTTP layer: authentication, authorization, rate limiting, routing and one consistent error format."""
import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Annotated

import psycopg
from fastapi import Depends, FastAPI, Header, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg_pool import PoolTimeout
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import auth, emailer, http_layer, jobs, ops, security, services, worker
from .config import COMPANY, DEFAULT_DATABASE_URL, POOL_MAX
from .db import create_pool
from .errors import AppError
from .http_layer import client_ip, error_response
from .invoice import build_invoice_pdf
from .logging_setup import current_context, setup_logging
from .migrate import run_migrations
from .schemas import (AuditOut, CancelRequest, ErrorResponse, HistoryList, LoginIn, LoginOut, MovementList,
                      NotificationDetail, NotificationList, OpsStatus, OrderCreate, OrderList, OrderOut,
                      PasswordChange, PasswordReset, ProductCreate, ProductList, ProductOut, ReportList,
                      ReportOut, RunNowOut, SecurityEventList, StatusUpdate, StockOut, StockUpdate, UserCreate,
                      UserList, UserOut, UserUpdate)
from .settings import ConfigError, Settings

log = logging.getLogger("inventory.app")

DESCRIPTION = """
Inventory, order and invoicing backend.

## Quick start
1. `POST /auth/login` with your email and password. Copy the `access_token` from the response.
2. Click **Authorize** (top right) and paste the token. Every other endpoint needs it.
3. Orders need an `Idempotency-Key` header (any unique string, e.g. a UUID). Sending the same key again
   returns the original order instead of creating a duplicate.

## Roles
| Role | Can do |
|---|---|
| `viewer` | Read products, stock, orders, invoices |
| `staff` | Everything a viewer can, plus create products, adjust stock, place / advance / cancel orders |
| `admin` | Everything, plus users, reports, notifications, operations status, audit data |

## Errors
Every error has the same shape: `{"error": {"code", "message", "details": [...], "request_id"}}`.
Quote the `request_id` when reporting a problem; it identifies the matching log lines.

| Status | Meaning |
|---|---|
| 401 | Missing, invalid or expired token (or wrong login) |
| 403 | Your role is not allowed to do this |
| 404 / 409 / 422 | Not found / conflicts with current data (e.g. insufficient stock) / invalid input |
| 429 | Rate limit hit. Wait `Retry-After` seconds |
| 503 | Temporarily busy or database unavailable. Safe to retry (orders: with the same Idempotency-Key) |

## Rate limits (per process)
Login 10/min per address, writes 120/min and reads 600/min per user, 1200/min per address overall.
"""

TAGS = [
    {"name": "System", "description": "Liveness check."},
    {"name": "Auth", "description": "Sign in and out, your own profile and password."},
    {"name": "Users", "description": "Account management (admin only)."},
    {"name": "Products", "description": "Catalogue."},
    {"name": "Stock", "description": "Stock levels, adjustments and the movement ledger."},
    {"name": "Orders", "description": "Place orders, move them through their lifecycle, cancel, download invoices."},
    {"name": "Notifications", "description": "Email queue inspection and recovery (admin only)."},
    {"name": "Operations", "description": "Worker health and scheduled jobs (admin only)."},
    {"name": "Reports", "description": "Stored daily reports (admin only)."},
    {"name": "Audit", "description": "Inventory consistency check and security event log (admin only)."},
]

ERR = {401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
       409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}}
PUBLIC_ERR = {422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}}


def create_app(database_url: str | None = None, auto_migrate: bool = True, run_worker: bool | None = None,
               settings: Settings | None = None, pool_max: int | None = None) -> FastAPI:
    """run_worker: start the background worker (emails, scheduled jobs) inside this process.
    Default comes from RUN_WORKER_IN_API (on unless set to 0)."""
    settings = settings or Settings.from_env()
    setup_logging(settings.log_format, settings.log_level)
    database_url = database_url or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    email_backend = os.environ.get("EMAIL_BACKEND", "file").lower()
    errors, warnings = settings.problems(database_url, email_backend)
    for warning in warnings:
        log.warning("Configuration warning: %s", warning)
    if errors:
        raise ConfigError("Unsafe or invalid configuration:\n - " + "\n - ".join(errors))
    if run_worker is None:
        run_worker = os.environ.get("RUN_WORKER_IN_API", "1") == "1"

    pool = create_pool(database_url, max_size=pool_max or POOL_MAX, timeout=settings.pool_timeout)
    limiter = security.RateLimiter()
    rates = {name: security.parse_rate(getattr(settings, f"rate_{name}")) for name in ("ip", "read", "write", "login")}

    def too_many(retry: int) -> AppError:
        return AppError(429, "RATE_LIMITED", "Too many requests. Please slow down.",
                        [{"retry_after_seconds": retry}], headers={"Retry-After": str(retry)})

    def ip_limit(request: Request) -> None:
        allowed, retry = limiter.check(f"ip:{client_ip(request, settings)}", *rates["ip"])
        if not allowed:
            raise too_many(retry)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        log.info("Starting Inventory API: %s", settings.summary(database_url, email_backend))
        try:                                    # fail fast with the real reason instead of a 30 s pool timeout
            with psycopg.connect(database_url, connect_timeout=5):
                pass
        except psycopg.OperationalError as exc:
            raise RuntimeError(f"Cannot connect to PostgreSQL. Check DATABASE_URL in .env.\n{exc}") from exc
        pool.open(wait=True)
        if auto_migrate:
            with pool.connection() as conn:
                applied = run_migrations(conn)
                if applied:
                    log.info("Applied migrations: %s", applied)
        stop, thread = threading.Event(), None
        if run_worker:
            # Built before the thread starts so a bad config stops the app loudly instead of failing silently.
            backend, job_settings = emailer.backend_from_env(), jobs.Settings.from_env()
            job_settings.validate()
            thread = threading.Thread(target=worker.run_forever, name="inventory-worker", daemon=True,
                                      kwargs={"url": database_url, "stop": stop, "backend": backend,
                                              "settings": job_settings})
            thread.start()
        yield
        stop.set()
        if thread is not None:
            thread.join(timeout=15)
        pool.close()

    docs = "/docs" if settings.docs_enabled else None
    app = FastAPI(title="Inventory Management API", version="5.0.0", description=DESCRIPTION, openapi_tags=TAGS,
                  lifespan=lifespan, dependencies=[Depends(ip_limit)], docs_url=docs,
                  redoc_url="/redoc" if settings.docs_enabled else None,
                  openapi_url="/openapi.json" if settings.docs_enabled else None)
    app.state.pool, app.state.limiter, app.state.settings = pool, limiter, settings
    http_layer.install(app, settings)

    def get_conn():
        with pool.connection() as conn:
            yield conn

    Conn = Annotated[psycopg.Connection, Depends(get_conn)]
    bearer = HTTPBearer(auto_error=False, description="Paste the `access_token` returned by POST /auth/login.")

    # ---------- authentication and authorization ----------
    def current_user(conn: Conn, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> dict:
        if credentials is None:
            raise auth.unauthorized("Authentication required. Send the header 'Authorization: Bearer <token>'.")
        user = auth.user_for_token(conn, credentials.credentials)
        if user is None:
            raise auth.unauthorized("Invalid or expired token. Sign in again.", "INVALID_TOKEN")
        current_context()["user_id"] = user["id"]            # lets the access log say who made the request
        return user

    def require(permission: str | None):
        """Dependency factory: signed in + allowed to do `permission` (None = any signed-in user) + not rate limited."""
        def dependency(request: Request, user: Annotated[dict, Depends(current_user)]) -> dict:
            if permission is not None and not security.has_permission(user["role"], permission):
                log.warning("Permission denied", extra={"event": "forbidden", "permission": permission,
                                                        "role": user["role"], "method": request.method,
                                                        "path": request.url.path})
                raise AppError(403, "FORBIDDEN", f"Your role ('{user['role']}') is not allowed to do this.",
                               [{"permission": permission}])
            bucket = "read" if request.method in ("GET", "HEAD", "OPTIONS") else "write"
            allowed, retry = limiter.check(f"user:{user['id']}:{bucket}", *rates[bucket])
            if not allowed:
                raise too_many(retry)
            return user
        return dependency

    Signed = Annotated[dict, Depends(require(None))]
    ProductId = Annotated[int, Path(ge=1, description="Product id")]
    OrderId = Annotated[int, Path(ge=1, description="Order id")]
    NotificationId = Annotated[int, Path(ge=1, description="Notification id")]
    Paging = {"limit": Annotated[int, Query(ge=1, le=100)], "offset": Annotated[int, Query(ge=0)]}

    # ---------- error handlers (every error uses the same JSON shape) ----------
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return error_response(exc.status_code, exc.code, exc.message, exc.details, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        details = []
        for e in exc.errors():
            loc = [str(p) for p in e["loc"]]
            details.append({"in": loc[0], "field": ".".join(loc[1:]) or loc[0],
                            "issue": e["msg"].removeprefix("Value error, ")})
        return error_response(422, "VALIDATION_ERROR", "The request is invalid. See details.", details)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        code = {400: "BAD_REQUEST", 404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        return error_response(exc.status_code, code, str(exc.detail), headers=dict(exc.headers or {}))

    @app.exception_handler(psycopg.errors.LockNotAvailable)
    async def _lock_timeout(_: Request, exc: Exception):
        # Someone else is holding the same stock rows for too long: safe to retry (idempotent key!).
        return error_response(503, "BUSY", "The item is being updated by another request. Please retry.",
                              headers={"Retry-After": "1"})

    @app.exception_handler(PoolTimeout)
    async def _pool_timeout(_: Request, exc: Exception):
        return error_response(503, "BUSY", "The service is under heavy load. Please retry.",
                              headers={"Retry-After": "1"})

    @app.exception_handler(psycopg.OperationalError)
    async def _db_down(_: Request, exc: Exception):
        log.error("Database unavailable: %s", exc, extra={"event": "database_unavailable"})
        return error_response(503, "DATABASE_UNAVAILABLE", "The database is temporarily unavailable.",
                              headers={"Retry-After": "5"})

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception):
        log.exception("Unhandled error")
        return error_response(500, "INTERNAL_ERROR", "Something went wrong on our side.")

    # ---------- system ----------
    @app.get("/health", tags=["System"], summary="Liveness and database check (public)")
    def health(conn: Conn):
        conn.execute("SELECT 1")
        return {"status": "ok"}

    # ---------- auth ----------
    @app.post("/auth/login", response_model=LoginOut, tags=["Auth"], responses=PUBLIC_ERR | {401: {"model": ErrorResponse}},
              summary="Sign in and get an access token (public)")
    def login(body: LoginIn, request: Request, conn: Conn):
        """Returns a bearer token valid for TOKEN_TTL_HOURS. After repeated wrong passwords the account is
        locked for a while. Failures always return the same message, so account existence is not revealed."""
        ip = client_ip(request, settings)
        allowed, retry = limiter.check(f"login:{ip}", *rates["login"])
        if not allowed:
            raise too_many(retry)
        return auth.login(conn, body.email, body.password, ip, request.headers.get("user-agent", "")[:200], settings)

    @app.post("/auth/logout", status_code=204, tags=["Auth"], responses=ERR, summary="Sign out (revokes this token)")
    def logout(user: Signed, conn: Conn, request: Request):
        auth.logout(conn, user, client_ip(request, settings))
        return Response(status_code=204)

    @app.get("/auth/me", response_model=UserOut, tags=["Auth"], responses=ERR, summary="Who am I?")
    def me(user: Signed, conn: Conn):
        return auth.get_user(conn, user["id"])

    @app.post("/auth/change-password", status_code=204, tags=["Auth"], responses=ERR,
              summary="Change your password (signs out your other sessions)")
    def change_password(body: PasswordChange, user: Signed, conn: Conn, request: Request):
        auth.change_password(conn, user, body.current_password, body.new_password, client_ip(request, settings))
        return Response(status_code=204)

    # ---------- users (admin) ----------
    @app.post("/users", response_model=UserOut, status_code=201, tags=["Users"], responses=ERR, summary="Create a user")
    def create_user(body: UserCreate, conn: Conn, admin: Annotated[dict, Depends(require("users:manage"))]):
        return auth.create_user(conn, body.email, body.full_name, body.password, body.role, admin["id"])

    @app.get("/users", response_model=UserList, tags=["Users"], responses=ERR, summary="List users",
             dependencies=[Depends(require("users:manage"))])
    def list_users(conn: Conn):
        return auth.list_users(conn)

    @app.patch("/users/{user_id}", response_model=UserOut, tags=["Users"], responses=ERR,
               summary="Change a user's name, role or active flag (deactivating signs them out at once)")
    def update_user(user_id: Annotated[int, Path(ge=1)], body: UserUpdate, conn: Conn,
                    admin: Annotated[dict, Depends(require("users:manage"))]):
        return auth.update_user(conn, user_id, admin["id"], body.full_name, body.role, body.is_active)

    @app.post("/users/{user_id}/reset-password", response_model=UserOut, tags=["Users"], responses=ERR,
              summary="Set a new password for a user (signs them out everywhere, clears any lock)")
    def reset_password(user_id: Annotated[int, Path(ge=1)], body: PasswordReset, conn: Conn,
                       admin: Annotated[dict, Depends(require("users:manage"))]):
        return auth.admin_reset_password(conn, user_id, body.new_password, admin["id"])

    # ---------- products and stock ----------
    @app.post("/products", response_model=ProductOut, status_code=201, tags=["Products"], responses=ERR,
              summary="Create a product")
    def create_product(body: ProductCreate, conn: Conn, user: Annotated[dict, Depends(require("products:write"))]):
        return services.create_product(conn, body.sku, body.name, body.price, body.stock,
                                       body.low_stock_threshold, user["id"])

    @app.get("/products", response_model=ProductList, tags=["Products"], responses=ERR, summary="List products",
             dependencies=[Depends(require("products:read"))])
    def list_products(conn: Conn, limit: Annotated[int, Query(ge=1, le=100)] = 50,
                      offset: Annotated[int, Query(ge=0)] = 0):
        return services.list_products(conn, limit, offset)

    @app.get("/products/{product_id}", response_model=ProductOut, tags=["Products"], responses=ERR,
             summary="Get one product", dependencies=[Depends(require("products:read"))])
    def get_product(product_id: ProductId, conn: Conn):
        return services.get_product(conn, product_id)

    @app.get("/products/{product_id}/stock", response_model=StockOut, tags=["Stock"], responses=ERR,
             summary="Current stock", dependencies=[Depends(require("stock:read"))])
    def get_stock(product_id: ProductId, conn: Conn):
        return services.get_stock(conn, product_id)

    @app.patch("/products/{product_id}/stock", response_model=StockOut, tags=["Stock"], responses=ERR,
               summary="Set stock (`set_to`) or adjust it (`adjust_by`); stock can never go below zero")
    def update_stock(product_id: ProductId, body: StockUpdate, conn: Conn,
                     user: Annotated[dict, Depends(require("stock:write"))]):
        return services.update_stock(conn, product_id, body.set_to, body.adjust_by, user["id"])

    @app.get("/products/{product_id}/movements", response_model=MovementList, tags=["Stock"], responses=ERR,
             summary="Stock ledger for a product (every change, with who and why)",
             dependencies=[Depends(require("stock:read"))])
    def list_movements(product_id: ProductId, conn: Conn, limit: Annotated[int, Query(ge=1, le=100)] = 50,
                       offset: Annotated[int, Query(ge=0)] = 0):
        return services.list_movements(conn, product_id, limit, offset)

    # ---------- orders ----------
    @app.post("/orders", response_model=OrderOut, status_code=201, tags=["Orders"], responses=ERR,
              summary="Place an order (needs an Idempotency-Key header)")
    def create_order(
        body: OrderCreate,
        response: Response,
        conn: Conn,
        user: Annotated[dict, Depends(require("orders:write"))],
        idempotency_key: Annotated[str | None, Header(
            description="Unique key per order attempt (e.g. a UUID). Retrying with the same key "
                        "returns the original order instead of creating a duplicate.")] = None,
    ):
        """Totals are always calculated on the server. All lines succeed together or none does.
        201 = created, 200 + `Idempotent-Replay: true` = this key was already used with the same request."""
        key = (idempotency_key or "").strip()
        if not key:
            raise AppError(400, "IDEMPOTENCY_KEY_REQUIRED", "The Idempotency-Key header is required.",
                           [{"in": "header", "field": "Idempotency-Key", "issue": "Missing"}])
        if len(key) > 255:
            raise AppError(400, "IDEMPOTENCY_KEY_INVALID", "The Idempotency-Key header is too long (max 255).",
                           [{"in": "header", "field": "Idempotency-Key", "issue": "Too long"}])
        order, replayed = services.create_order(conn, key, [(i.product_id, i.quantity) for i in body.items],
                                                body.customer.model_dump(), user["id"])
        if replayed:
            response.status_code = 200
            response.headers["Idempotent-Replay"] = "true"
        return order

    @app.get("/orders", response_model=OrderList, tags=["Orders"], responses=ERR, summary="List orders",
             dependencies=[Depends(require("orders:read"))])
    def list_orders(conn: Conn,
                    status: Annotated[str | None, Query(pattern="^(confirmed|processing|shipped|completed|cancelled)$")] = None,
                    limit: Annotated[int, Query(ge=1, le=100)] = 50, offset: Annotated[int, Query(ge=0)] = 0):
        return services.list_orders(conn, status, limit, offset)

    @app.get("/orders/{order_id}", response_model=OrderOut, tags=["Orders"], responses=ERR, summary="Get one order",
             dependencies=[Depends(require("orders:read"))])
    def get_order(order_id: OrderId, conn: Conn):
        return services.get_order(conn, order_id)

    @app.patch("/orders/{order_id}/status", response_model=OrderOut, tags=["Orders"], responses=ERR,
               summary="Advance an order: confirmed -> processing -> shipped -> completed")
    def update_order_status(order_id: OrderId, body: StatusUpdate, conn: Conn,
                            user: Annotated[dict, Depends(require("orders:write"))]):
        return services.transition_order(conn, order_id, body.status, body.note, user["id"])

    @app.post("/orders/{order_id}/cancel", response_model=OrderOut, tags=["Orders"], responses=ERR,
              summary="Cancel an order that has not shipped; its stock is returned atomically")
    def cancel_order(order_id: OrderId, conn: Conn, user: Annotated[dict, Depends(require("orders:write"))],
                     body: CancelRequest | None = None):
        return services.transition_order(conn, order_id, "cancelled", body.reason if body else None, user["id"])

    @app.get("/orders/{order_id}/history", response_model=HistoryList, tags=["Orders"], responses=ERR,
             summary="Status history with who made each change", dependencies=[Depends(require("orders:read"))])
    def order_history(order_id: OrderId, conn: Conn):
        return services.get_order_history(conn, order_id)

    @app.get("/orders/{order_id}/invoice.pdf", tags=["Orders"], summary="Download the invoice as a PDF",
             responses={200: {"content": {"application/pdf": {}}}, 401: {"model": ErrorResponse},
                        403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
             dependencies=[Depends(require("orders:read"))])
    def order_invoice(order_id: OrderId, conn: Conn):
        order = services.get_order(conn, order_id)
        return Response(content=build_invoice_pdf(order, COMPANY), media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{order["invoice_number"]}.pdf"'})

    # ---------- notifications, operations, reports, audit (admin) ----------
    @app.get("/notifications", response_model=NotificationList, tags=["Notifications"], responses=ERR,
             summary="Email queue", dependencies=[Depends(require("notifications:manage"))])
    def list_notifications(conn: Conn,
                           status: Annotated[str | None, Query(pattern="^(pending|sent|failed|skipped)$")] = None,
                           limit: Annotated[int, Query(ge=1, le=200)] = 50):
        return services.list_notifications(conn, status, limit)

    @app.get("/notifications/{notification_id}", response_model=NotificationDetail, tags=["Notifications"],
             responses=ERR, summary="One notification with every delivery attempt",
             dependencies=[Depends(require("notifications:manage"))])
    def get_notification(notification_id: NotificationId, conn: Conn):
        return ops.get_notification(conn, notification_id)

    @app.post("/notifications/{notification_id}/retry", response_model=NotificationDetail, tags=["Notifications"],
              responses=ERR, summary="Re-queue a failed notification (after fixing the cause)",
              dependencies=[Depends(require("notifications:manage"))])
    def retry_notification(notification_id: NotificationId, conn: Conn):
        return ops.retry_notification(conn, notification_id)

    @app.post("/notifications/{notification_id}/dismiss", response_model=NotificationDetail, tags=["Notifications"],
              responses=ERR, summary="Clear a failed notification that can never succeed (e.g. bad address)",
              dependencies=[Depends(require("notifications:manage"))])
    def dismiss_notification(notification_id: NotificationId, conn: Conn):
        return ops.dismiss_notification(conn, notification_id)

    @app.get("/ops/status", response_model=OpsStatus, tags=["Operations"], responses=ERR,
             summary="Worker alive?, email queue, scheduled jobs, recent runs",
             dependencies=[Depends(require("ops:read"))])
    def ops_status(conn: Conn):
        return ops.ops_status(conn)

    @app.post("/ops/schedules/{name}/run-now", response_model=RunNowOut, tags=["Operations"], responses=ERR,
              summary="Run a scheduled job now (daily_report, housekeeping)",
              dependencies=[Depends(require("ops:write"))])
    def run_schedule_now(name: str, conn: Conn):
        return ops.run_schedule_now(conn, name)

    @app.get("/reports", response_model=ReportList, tags=["Reports"], responses=ERR, summary="Stored daily reports",
             dependencies=[Depends(require("reports:read"))])
    def list_reports(conn: Conn, limit: Annotated[int, Query(ge=1, le=100)] = 20):
        return ops.list_reports(conn, limit)

    @app.get("/reports/{report_id}", response_model=ReportOut, tags=["Reports"], responses=ERR,
             summary="One stored report", dependencies=[Depends(require("reports:read"))])
    def get_report(report_id: Annotated[int, Path(ge=1)], conn: Conn):
        return ops.get_report(conn, report_id)

    @app.get("/inventory/audit", response_model=AuditOut, tags=["Audit"], responses=ERR,
             summary="Check that every product's stock equals the sum of its ledger",
             dependencies=[Depends(require("audit:read"))])
    def audit(conn: Conn):
        return services.audit_inventory(conn)

    @app.get("/security/events", response_model=SecurityEventList, tags=["Audit"], responses=ERR,
             summary="Recent security events (logins, lockouts, password changes, user changes)",
             dependencies=[Depends(require("audit:read"))])
    def security_events(conn: Conn, limit: Annotated[int, Query(ge=1, le=200)] = 50):
        return auth.list_security_events(conn, limit)

    return app


app = create_app()
