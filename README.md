# Inventory Management API — Phase 4 (automated operations)

FastAPI + PostgreSQL. Phase 1 core API, 2 database + locking + idempotency, 3 order lifecycle + invoices + emails,
**4 background automation: durable email delivery, low-stock alerts, scheduled reports, safe retries, health checks.**

## Setup (Windows, no Docker)
```powershell
copy .env.example .env          # edit DATABASE_URL, COMPANY_NAME, ADMIN_EMAIL / REPORT_RECIPIENTS, SMTP_* ...
pip install -r requirements-dev.txt
uvicorn app.main:app            # ONE command: API + background worker + migrations
```
That is all: the worker runs inside the API process (`RUN_WORKER_IN_API=1`, the default). For a separate worker
process (e.g. on another machine) set `RUN_WORKER_IN_API=0` on the API and run `python -m app.worker`.
Any number of API/worker processes can run at once; they coordinate through the database and never double-process.
To keep it running without anyone logged in, run uvicorn as a Windows service (e.g. with NSSM) or Task Scheduler "at startup".

## What now happens automatically
| Routine task | How |
|---|---|
| Customer emails (confirmed / shipped / completed / cancelled, invoice attached) | Queued with the order, delivered by the worker |
| Low-stock alert | Queued when stock *crosses* down to the alert level (one per crossing) → `ADMIN_EMAIL` |
| **Daily report** at `REPORT_TIME` (default 08:00 `REPORT_TIMEZONE`) | Orders, net revenue, top products, stock to reorder, ledger audit, email-queue health → `REPORT_RECIPIENTS`; also stored (`GET /reports`) |
| **Housekeeping** at `HOUSEKEEPING_TIME` (default 03:00) | Removes idempotency keys older than 30 days and delivered notifications older than 90 days (failed/pending are never removed) |
| Worker health | Heartbeat every few seconds → `GET /ops/status` says whether a worker is alive |

## Failure handling
* **An email problem can never undo or delay an order.** The API never talks to a mail server; it only writes a notification row in the order's own transaction.
* **Every attempt is recorded** (`notification_attempts`: time, outcome, error text). See `GET /notifications/{id}`.
* **Temporary failures retry with backoff** (1, 2, 4, 8, 16 min), then the notification is marked `failed`.
* **Permanent failures give up at once** (bad recipient address, missing data): no pointless retries.
* **A bad notification never blocks the queue**: it is pushed back/failed and the next one proceeds.
* **After you fix the cause** (e.g. SMTP password) re-queue with `POST /notifications/{id}/retry`.
* **Crashes lose nothing**: each item is processed in its own transaction (`FOR UPDATE SKIP LOCKED`); if a worker dies mid-send the item simply becomes due again (at-least-once, so in a rare crash window one email may arrive twice).
* **Scheduled jobs run exactly once** per slot even with several workers (row lock + next-run advanced in the same transaction as the job's output).
* **A failing scheduled job leaves nothing half-done** (savepoint rollback), is recorded (`schedules.last_error`, `schedule_runs`) and retried after 2, 4, 8 … up to 60 minutes, not tomorrow.
* **Downtime is covered**: after the worker was off for days, one catch-up report covers everything since the previous report: no gaps, no double counting.

## Operations endpoints
| Endpoint | Purpose |
|---|---|
| `GET /ops/status` | Worker alive?, notifications pending/failed/skipped, schedules (next run, last error), last 10 job runs |
| `POST /ops/schedules/daily_report/run-now` | Run a job now (the worker does it within seconds); also `housekeeping` |
| `GET /reports`, `GET /reports/{id}` | Stored daily reports (JSON) |
| `GET /notifications?status=failed`, `GET /notifications/{id}` | Queue contents, with the full attempt log |
| `POST /notifications/{id}/retry` | Re-queue a failed notification |

Try it: place an order, call `POST /ops/schedules/daily_report/run-now`, wait a few seconds, open the new `.eml` in `outbox/`.

## Email settings
`EMAIL_BACKEND=file` writes `.eml` files to `outbox/` (default, no server needed). For real mail set `EMAIL_BACKEND=smtp`
and `SMTP_HOST/PORT/USER/PASSWORD` (Gmail needs an *app password*; keep `EMAIL_FROM` equal to `SMTP_USER`).
A bad config (e.g. `smtp` without `SMTP_HOST`, unknown time zone) stops the app at startup with a clear message.

## Earlier phases (unchanged behaviour)
Lifecycle `confirmed → processing → shipped → completed` with cancel until shipped (stock restored atomically),
PDF invoices at `GET /orders/{id}/invoice.pdf`, `Idempotency-Key` header required on `POST /orders`, row-level stock locking.

## Tests
```powershell
pytest -v        # needs TEST_DATABASE_URL (database name must end in _test)
```
DB-free tests (`test_invoice`, `test_emailer`, `test_timeutil`, `test_report_email`) run without PostgreSQL.
`python scripts/load_test.py` (server running) re-proves no overselling/duplicates over HTTP.
Note: the load test places ~110 orders, and the worker writes an email file for each into `outbox/`; delete them afterwards.

## Upgrading from Phase 3
Migration `003_automation.sql` runs automatically. `pip install -r requirements.txt` (adds `tzdata`, needed for time zones on Windows).
