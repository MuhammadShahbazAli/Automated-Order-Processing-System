"""Request/response models. Requests reject unknown fields, so a client can never
sneak in its own `price` or `total` -- those are always computed on the server."""
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .limits import MAX_LINE_QTY, MAX_ORDER_LINES, MAX_STOCK
from .validation import EMAIL_PATTERN


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------- requests ----------
class ProductCreate(_Strict):
    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    stock: int = Field(default=0, strict=True, ge=0, le=MAX_STOCK)
    low_stock_threshold: int = Field(default=5, strict=True, ge=0, le=MAX_STOCK)


class StockUpdate(_Strict):
    set_to: int | None = Field(default=None, strict=True, ge=0, le=MAX_STOCK)
    adjust_by: int | None = Field(default=None, strict=True, ge=-MAX_STOCK, le=MAX_STOCK)

    @model_validator(mode="after")
    def exactly_one(self):
        if (self.set_to is None) == (self.adjust_by is None):
            raise ValueError("Provide exactly one of 'set_to' or 'adjust_by'")
        return self


class OrderItemIn(_Strict):
    product_id: int = Field(strict=True, ge=1)
    quantity: int = Field(strict=True, ge=1, le=MAX_LINE_QTY)


class CustomerIn(_Strict):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(max_length=254, pattern=EMAIL_PATTERN)
    phone: str | None = Field(default=None, max_length=40)
    address: str | None = Field(default=None, max_length=500)


class OrderCreate(_Strict):
    customer: CustomerIn
    items: list[OrderItemIn] = Field(min_length=1, max_length=MAX_ORDER_LINES)


class StatusUpdate(_Strict):
    status: Literal["processing", "shipped", "completed", "cancelled"]
    note: str | None = Field(default=None, max_length=500)


class CancelRequest(_Strict):
    reason: str | None = Field(default=None, max_length=500)


# ---------- responses ----------
class ProductOut(BaseModel):
    id: int
    sku: str
    name: str
    price: str
    stock: int
    low_stock_threshold: int
    created_at: datetime
    updated_at: datetime


class ProductList(BaseModel):
    items: list[ProductOut]
    total: int
    limit: int
    offset: int


class StockOut(BaseModel):
    product_id: int
    stock: int
    previous_stock: int | None = None


class OrderLineOut(BaseModel):
    product_id: int
    sku: str
    name: str
    unit_price: str
    quantity: int
    line_total: str


class CustomerOut(BaseModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None


class OrderOut(BaseModel):
    id: int
    invoice_number: str
    status: str
    created_at: datetime
    updated_at: datetime
    cancelled_at: datetime | None = None
    cancel_reason: str | None = None
    customer: CustomerOut | None = None
    items: list[OrderLineOut]
    total: str


class OrderSummary(BaseModel):
    id: int
    invoice_number: str
    status: str
    customer_name: str | None
    customer_email: str | None
    total: str
    created_at: datetime


class OrderList(BaseModel):
    items: list[OrderSummary]
    total: int
    limit: int
    offset: int


class HistoryOut(BaseModel):
    id: int
    from_status: str | None
    to_status: str
    note: str | None
    created_at: datetime


class HistoryList(BaseModel):
    items: list[HistoryOut]


class MovementOut(BaseModel):
    id: int
    product_id: int
    change: int
    stock_after: int
    reason: str
    order_id: int | None
    created_at: datetime


class MovementList(BaseModel):
    items: list[MovementOut]
    total: int
    limit: int
    offset: int


class NotificationOut(BaseModel):
    id: int
    type: str
    payload: dict
    recipient: str | None
    status: str
    attempts: int
    last_error: str | None
    created_at: datetime
    next_attempt_at: datetime
    sent_at: datetime | None


class NotificationList(BaseModel):
    items: list[NotificationOut]


class AttemptOut(BaseModel):
    attempt: int
    outcome: str
    permanent: bool
    error: str | None
    created_at: datetime


class NotificationDetail(NotificationOut):
    attempt_log: list[AttemptOut]


class ReportOut(BaseModel):
    id: int
    kind: str
    period_start: datetime
    period_end: datetime
    created_at: datetime
    data: dict


class ReportList(BaseModel):
    items: list[ReportOut]


class OpsStatus(BaseModel):
    worker: dict
    notifications: dict
    schedules: list[dict]
    recent_runs: list[dict]


class RunNowOut(BaseModel):
    name: str
    next_run_at: datetime


class AuditOut(BaseModel):
    consistent: bool
    discrepancies: list[dict]


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[dict] = []


class ErrorResponse(BaseModel):
    error: ErrorBody
