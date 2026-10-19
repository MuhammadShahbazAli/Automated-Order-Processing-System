# Inventory Management API — Phase 2 (PostgreSQL)

## Setup
```bash
docker compose up -d db                 # PostgreSQL 16 + an empty inventory_test database
copy .env.example .env                  # Windows (use `cp` on Mac/Linux)
pip install -r requirements-dev.txt
uvicorn app.main:app --reload           # applies migrations/ automatically on startup
```
No Docker? Install PostgreSQL, create databases `inventory` and `inventory_test`, and edit the URLs in `.env`.
Manual migration: `python -m app.migrate`. API docs: http://127.0.0.1:8000/docs

## Prove it works
```bash
pytest -v                                # integration tests (need TEST_DATABASE_URL, see .env.example)
python scripts/load_test.py              # 100 simultaneous HTTP buyers vs 10 units, plus a 30x duplicate-key storm
```

## Tables
`products`, `orders`, `order_items` (price/name snapshot), `inventory_movements` (append-only ledger),
`notifications` (outbox), `idempotency_keys`, `schema_migrations`.

## How overselling is prevented
| Risk | Defence |
|---|---|
| Two orders read the same stock and both succeed | `SELECT ... FOR UPDATE` on the product rows; the second order waits, then reads the *new* stock |
| Deadlock between orders with items in different order | Rows are always locked in `id` order |
| Request hangs forever behind a lock | `lock_timeout` (default 5s) -> HTTP 503 + `Retry-After` |
| Partial orders | One transaction: idempotency claim, stock, order, items, ledger, notifications commit together or not at all |
| Bug in app code | DB `CHECK (stock >= 0)`, guarded `UPDATE ... WHERE stock >= qty`, FK constraints |
| Silent stock drift | Ledger invariant `products.stock = SUM(movements)`; check any time with `GET /inventory/audit` |

## Idempotency (`POST /orders`)
Send a unique `Idempotency-Key` header (e.g. a UUID) per order attempt. Required: missing -> 400.
- Retry with the same key + same body -> original order returned, **HTTP 200** + `Idempotent-Replay: true`, nothing re-deducted.
- Same key + different body -> 422 `IDEMPOTENCY_KEY_REUSED`.
- A failed attempt (409/404) does **not** burn the key: the claim rolls back with the transaction, so it can be retried.
- Simultaneous duplicates: the second request waits on the first, then replays its result.
- Keys are global for now; scope them per client once authentication exists.

## Other endpoints added in Phase 2
`GET /products/{id}/movements`, `GET /notifications?status=pending`, `GET /inventory/audit`.
Notifications are only *recorded* (`order_confirmed`, `low_stock` once per threshold crossing); delivery is a later phase.
