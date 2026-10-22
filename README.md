# Inventory Management API — Phase 3 (order lifecycle)

FastAPI + PostgreSQL. Phase 1: core API. Phase 2: PostgreSQL, locking, idempotency.
**Phase 3: order statuses, cancellation with stock restoration, PDF invoices, customer emails.**

## Setup (Windows, no Docker)
```powershell
copy .env.example .env          # then edit DATABASE_URL, COMPANY_NAME, CURRENCY ...
pip install -r requirements-dev.txt
uvicorn app.main:app            # Terminal 1 (applies migrations 001 + 002 automatically)
python -m app.worker            # Terminal 2: delivers emails (leave running)
```
Docs: http://127.0.0.1:8000/docs.  Emails land as `.eml` files in `outbox/` until you set `EMAIL_BACKEND=smtp`.

## The order lifecycle
```
            confirmed ──► processing ──► shipped ──► completed
                │              │
                └──► cancelled ◄┘          (cancel is NOT allowed once shipped)
```
| Action | Endpoint | Effect |
|---|---|---|
| Submit | `POST /orders` (needs `Idempotency-Key` header and `customer`) | Stock reserved, invoice number assigned, confirmation email queued |
| Advance | `PATCH /orders/{id}/status` `{"status": "processing"\|"shipped"\|"completed", "note": "..."}` | Status history row; shipped/completed emails queued |
| Cancel | `POST /orders/{id}/cancel` `{"reason": "..."}` (body optional) | **Stock returned in the same transaction**, ledger row `order_cancelled`, cancellation email queued |
| Invoice | `GET /orders/{id}/invoice.pdf` | A4 PDF; cancelled orders are stamped CANCELLED |
| Track | `GET /orders`, `GET /orders/{id}`, `GET /orders/{id}/history` | List (filter `?status=`), detail, full status history |

Example order body:
```json
{"customer": {"name": "Ayesha Khan", "email": "ayesha@example.com", "phone": "+92 321 1234567", "address": "12 Mall Road, Lahore"},
 "items": [{"product_id": 1, "quantity": 2}]}
```
Invalid moves (e.g. `confirmed -> completed`, cancelling a shipped order, cancelling twice) return **409 `INVALID_STATUS_TRANSITION`** listing the allowed next steps.

## Why cancellation can't double-restore or race
Every status change locks the **order row** (`FOR UPDATE`) first; cancellation then locks the product rows in id order.
Ten simultaneous cancels, or a cancel racing a shipment, serialize: exactly one wins and stock is restored at most once.
The ledger invariant (`stock = sum of movements`) still holds: check with `GET /inventory/audit`.

## Emails (`python -m app.worker`)
The API only *records* notifications inside the order's transaction; the worker delivers them, so a broken mail server
can never fail or slow an order. Failed sends retry with backoff (1, 2, 4, 8, 16 min), then are marked `failed`.
Several workers can run at once without double-sending (`FOR UPDATE SKIP LOCKED`). Delivery is at-least-once.
`python -m app.worker --once` delivers what is due and exits.

| Notification | Sent to | Invoice attached |
|---|---|---|
| `order_confirmed` | customer | yes |
| `order_shipped` | customer | no |
| `order_completed` | customer | yes |
| `order_cancelled` | customer | no |
| `low_stock` | `ADMIN_EMAIL` (skipped if unset) | no |

Real email: set `EMAIL_BACKEND=smtp` and `SMTP_HOST/PORT/USER/PASSWORD` (Gmail needs an *app password*).
Check queue state with `GET /notifications?status=pending|sent|failed|skipped`.

## Invoices
Generated on demand from the order's stored snapshot (name, price, SKU at purchase time), so the same order always
produces the same PDF. No third-party PDF library is used. Limits: built-in fonts cover Latin text only
(other scripts print as `?`); no tax/discount/payment lines yet; invoice number is `INV-` + order id.

## Tests
```powershell
pytest -v        # needs TEST_DATABASE_URL (database name must end in _test)
```
DB-free tests (`test_invoice.py`, `test_emailer.py`) run even without PostgreSQL.
`python scripts/load_test.py` (server running) re-proves no overselling and no duplicate orders over HTTP.

## Upgrading from Phase 2
Migration `002_order_lifecycle.sql` runs automatically; existing orders keep working (customer fields are empty,
their status history is back-filled). `POST /orders` now **requires** a `customer` object.
