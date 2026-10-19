"""Request/response models. Requests reject unknown fields, so a client can never
sneak in its own `price` or `total` -- those are always computed on the server."""
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .limits import MAX_LINE_QTY, MAX_ORDER_LINES, MAX_STOCK



class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------- requests ----------
class ProductCreate(_Strict):
    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    stock: int = Field(default=0, strict=True, ge=0, le=MAX_STOCK)


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


class OrderCreate(_Strict):
    items: list[OrderItemIn] = Field(min_length=1, max_length=MAX_ORDER_LINES)


# ---------- responses ----------
class ProductOut(BaseModel):
    id: int
    sku: str
    name: str
    price: str
    stock: int
    created_at: str
    updated_at: str


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


class OrderOut(BaseModel):
    id: int
    status: str
    created_at: str
    items: list[OrderLineOut]
    total: str


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[dict] = []


class ErrorResponse(BaseModel):
    error: ErrorBody
