# Inventory Management API (core)

FastAPI + SQLite. Business rules live in `app/services.py`, HTTP in `app/main.py`.

## Run
```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```
Interactive docs: http://127.0.0.1:8000/docs  (DB file: `inventory.db`, override with `INVENTORY_DB`)

## Endpoints
| Method | Path | Purpose | Success |
|---|---|---|---|
| POST | `/products` | Create product `{sku, name, price, stock?}` | 201 |
| GET | `/products?limit=&offset=` | List products | 200 |
| GET | `/products/{id}` | Get one product | 200 |
| GET | `/products/{id}/stock` | View stock | 200 |
| PATCH | `/products/{id}/stock` | Update stock: `{"set_to": n}` **or** `{"adjust_by": ±n}` | 200 |
| POST | `/orders` | Submit order `{items:[{product_id, quantity}]}` | 201 |
| GET | `/orders/{id}` | View an order | 200 |

## Errors (always this shape)
```json
{"error": {"code": "INSUFFICIENT_STOCK", "message": "...", "details": [{"product_id": 2, "requested": 5, "available": 1}]}}
```
| Status | Code | When |
|---|---|---|
| 422 | `VALIDATION_ERROR` | Bad/missing fields, qty < 1, non-integer qty, unknown fields |
| 404 | `PRODUCT_NOT_FOUND` / `ORDER_NOT_FOUND` | ID does not exist |
| 409 | `DUPLICATE_SKU` / `INSUFFICIENT_STOCK` | Conflicts with current data |

## Key rules
- **Totals are server-side**: prices come from the database; clients cannot send `price` or `total` (rejected with 422).
- **Orders are all-or-nothing** and stock is deducted in the same transaction; no overselling under concurrency.
- Repeated product IDs in one order are merged into one line.
- Order lines snapshot name and price, so later price changes never alter past orders.
- Money is stored as integer cents and returned as strings (`"19.99"`).

## Tests
```bash
python -m unittest discover -s tests      # business logic, no extra packages
pip install -r requirements-dev.txt && pytest   # includes HTTP tests
```
