"""HTTP layer: routing, validation errors, and one consistent error format."""
import logging
import os
import sqlite3
from typing import Annotated

from fastapi import Depends, FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import services
from .db import connect, init_db
from .errors import AppError
from .schemas import (ErrorResponse, OrderCreate, OrderOut, ProductCreate, ProductList, ProductOut,
                      StockOut, StockUpdate)

log = logging.getLogger("inventory_api")

ERRORS = {404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}}


def _error(status: int, code: str, message: str, details: list[dict] | None = None) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": {"code": code, "message": message, "details": details or []}})


def create_app(db_path: str | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("INVENTORY_DB", "inventory.db")
    init_db(db_path)
    app = FastAPI(title="Inventory Management API", version="1.0.0")

    def get_conn():
        conn = connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
    ProductId = Annotated[int, Path(ge=1)]

    # ---------- error handlers (every error uses the same JSON shape) ----------
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return _error(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        details = []
        for e in exc.errors():
            loc = [str(p) for p in e["loc"]]
            details.append({
                "in": loc[0],
                "field": ".".join(loc[1:]) or loc[0],
                "issue": e["msg"].removeprefix("Value error, "),
            })
        return _error(422, "VALIDATION_ERROR", "The request is invalid. See details.", details)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        return _error(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception):
        log.exception("Unhandled error")
        return _error(500, "INTERNAL_ERROR", "Something went wrong on our side.")

    # ---------- routes ----------
    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/products", response_model=ProductOut, status_code=201, responses=ERRORS)
    def create_product(body: ProductCreate, conn: Conn):
        return services.create_product(conn, body.sku, body.name, body.price, body.stock)

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

    @app.post("/orders", response_model=OrderOut, status_code=201, responses=ERRORS)
    def create_order(body: OrderCreate, conn: Conn):
        return services.create_order(conn, [(i.product_id, i.quantity) for i in body.items])

    @app.get("/orders/{order_id}", response_model=OrderOut, responses=ERRORS)
    def get_order(order_id: Annotated[int, Path(ge=1)], conn: Conn):
        return services.get_order(conn, order_id)

    return app


app = create_app()
