"""HTTP layer: routing, validation errors, and one consistent error format."""
import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated

import psycopg
from fastapi import Depends, FastAPI, Header, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg_pool import PoolTimeout
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import services
from .config import COMPANY, DEFAULT_DATABASE_URL
from .db import create_pool
from .errors import AppError
from .invoice import build_invoice_pdf
from .migrate import run_migrations
from .schemas import (AuditOut, CancelRequest, ErrorResponse, HistoryList, MovementList, NotificationList,
                      OrderCreate, OrderList, OrderOut, ProductCreate, ProductList, ProductOut, StatusUpdate,
                      StockOut, StockUpdate)

log = logging.getLogger("inventory_api")
ERRORS = {404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}}


def _error(status: int, code: str, message: str, details: list[dict] | None = None,
           headers: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, headers=headers,
                        content={"error": {"code": code, "message": message, "details": details or []}})


def create_app(database_url: str | None = None, auto_migrate: bool = True) -> FastAPI:
    database_url = database_url or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    pool = create_pool(database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        pool.open(wait=True)
        if auto_migrate:
            with pool.connection() as conn:
                applied = run_migrations(conn)
                if applied:
                    log.info("Applied migrations: %s", applied)
        yield
        pool.close()

    app = FastAPI(title="Inventory Management API", version="3.0.0", lifespan=lifespan)

    def get_conn():
        with pool.connection() as conn:
            yield conn

    Conn = Annotated[psycopg.Connection, Depends(get_conn)]
    ProductId = Annotated[int, Path(ge=1)]
    OrderId = Annotated[int, Path(ge=1)]

    # ---------- error handlers (every error uses the same JSON shape) ----------
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return _error(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        details = []
        for e in exc.errors():
            loc = [str(p) for p in e["loc"]]
            details.append({"in": loc[0], "field": ".".join(loc[1:]) or loc[0],
                            "issue": e["msg"].removeprefix("Value error, ")})
        return _error(422, "VALIDATION_ERROR", "The request is invalid. See details.", details)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        return _error(exc.status_code, code, str(exc.detail))

    @app.exception_handler(psycopg.errors.LockNotAvailable)
    async def _lock_timeout(_: Request, exc: Exception):
        # Someone else is holding the same stock rows for too long: safe to retry (idempotent key!).
        return _error(503, "BUSY", "The item is being updated by another request. Please retry.",
                      headers={"Retry-After": "1"})

    @app.exception_handler(PoolTimeout)
    async def _pool_timeout(_: Request, exc: Exception):
        return _error(503, "BUSY", "The service is under heavy load. Please retry.", headers={"Retry-After": "1"})

    @app.exception_handler(psycopg.OperationalError)
    async def _db_down(_: Request, exc: Exception):
        log.error("Database unavailable: %s", exc)
        return _error(503, "DATABASE_UNAVAILABLE", "The database is temporarily unavailable.",
                      headers={"Retry-After": "5"})

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception):
        log.exception("Unhandled error")
        return _error(500, "INTERNAL_ERROR", "Something went wrong on our side.")

    # ---------- routes ----------
    @app.get("/health")
    def health(conn: Conn):
        conn.execute("SELECT 1")
        return {"status": "ok"}

    @app.post("/products", response_model=ProductOut, status_code=201, responses=ERRORS)
    def create_product(body: ProductCreate, conn: Conn):
        return services.create_product(conn, body.sku, body.name, body.price, body.stock,
                                       body.low_stock_threshold)

    @app.get("/products", response_model=ProductList, responses=ERRORS)
    def list_products(conn: Conn, limit: Annotated[int, Query(ge=1, le=100)] = 50,
                      offset: Annotated[int, Query(ge=0)] = 0):
        return services.list_products(conn, limit, offset)

    @app.get("/products/{product_id}", response_model=ProductOut, responses=ERRORS)
    def get_product(product_id: ProductId, conn: Conn):
        return services.get_product(conn, product_id)

    @app.get("/products/{product_id}/stock", response_model=StockOut, responses=ERRORS)
    def get_stock(product_id: ProductId, conn: Conn):
        return services.get_stock(conn, product_id)

    @app.patch("/products/{product_id}/stock", response_model=StockOut, responses=ERRORS)
    def update_stock(product_id: ProductId, body: StockUpdate, conn: Conn):
        return services.update_stock(conn, product_id, body.set_to, body.adjust_by)

    @app.get("/products/{product_id}/movements", response_model=MovementList, responses=ERRORS)
    def list_movements(product_id: ProductId, conn: Conn, limit: Annotated[int, Query(ge=1, le=100)] = 50,
                       offset: Annotated[int, Query(ge=0)] = 0):
        return services.list_movements(conn, product_id, limit, offset)

    @app.post("/orders", response_model=OrderOut, status_code=201, responses=ERRORS)
    def create_order(
        body: OrderCreate,
        response: Response,
        conn: Conn,
        idempotency_key: Annotated[str | None, Header(
            description="Unique key per order attempt (e.g. a UUID). Retrying with the same key "
                        "returns the original order instead of creating a duplicate.")] = None,
    ):
        key = (idempotency_key or "").strip()
        if not key:
            raise AppError(400, "IDEMPOTENCY_KEY_REQUIRED", "The Idempotency-Key header is required.",
                           [{"in": "header", "field": "Idempotency-Key", "issue": "Missing"}])
        if len(key) > 255:
            raise AppError(400, "IDEMPOTENCY_KEY_INVALID", "The Idempotency-Key header is too long (max 255).",
                           [{"in": "header", "field": "Idempotency-Key", "issue": "Too long"}])
        order, replayed = services.create_order(conn, key, [(i.product_id, i.quantity) for i in body.items],
                                                body.customer.model_dump())
        if replayed:
            response.status_code = 200
            response.headers["Idempotent-Replay"] = "true"
        return order

    @app.get("/orders", response_model=OrderList, responses=ERRORS)
    def list_orders(conn: Conn,
                    status: Annotated[str | None, Query(pattern="^(confirmed|processing|shipped|completed|cancelled)$")] = None,
                    limit: Annotated[int, Query(ge=1, le=100)] = 50, offset: Annotated[int, Query(ge=0)] = 0):
        return services.list_orders(conn, status, limit, offset)

    @app.get("/orders/{order_id}", response_model=OrderOut, responses=ERRORS)
    def get_order(order_id: OrderId, conn: Conn):
        return services.get_order(conn, order_id)

    @app.patch("/orders/{order_id}/status", response_model=OrderOut, responses=ERRORS)
    def update_order_status(order_id: OrderId, body: StatusUpdate, conn: Conn):
        """confirmed -> processing -> shipped -> completed. Cancel is allowed until shipped."""
        return services.transition_order(conn, order_id, body.status, body.note)

    @app.post("/orders/{order_id}/cancel", response_model=OrderOut, responses=ERRORS)
    def cancel_order(order_id: OrderId, conn: Conn, body: CancelRequest | None = None):
        """Cancel an order that has not shipped yet; its stock is returned in the same transaction."""
        return services.transition_order(conn, order_id, "cancelled", body.reason if body else None)

    @app.get("/orders/{order_id}/history", response_model=HistoryList, responses=ERRORS)
    def order_history(order_id: OrderId, conn: Conn):
        return services.get_order_history(conn, order_id)

    @app.get("/orders/{order_id}/invoice.pdf", responses={200: {"content": {"application/pdf": {}}},
                                                          404: {"model": ErrorResponse}})
    def order_invoice(order_id: OrderId, conn: Conn):
        order = services.get_order(conn, order_id)
        return Response(content=build_invoice_pdf(order, COMPANY), media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{order["invoice_number"]}.pdf"'})

    @app.get("/notifications", response_model=NotificationList, responses=ERRORS)
    def list_notifications(conn: Conn, status: Annotated[str | None, Query(pattern="^(pending|sent|failed|skipped)$")] = None,
                           limit: Annotated[int, Query(ge=1, le=200)] = 50):
        return services.list_notifications(conn, status, limit)

    @app.get("/inventory/audit", response_model=AuditOut, responses=ERRORS)
    def audit(conn: Conn):
        return services.audit_inventory(conn)

    return app


app = create_app()
