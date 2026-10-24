# Inventory Management API — Phase 5 (tested and secured)

FastAPI + PostgreSQL. Phases 1-4: core API, database + locking + idempotency, order lifecycle + invoices + emails,
background automation. **Phase 5 adds: authentication, role-based authorization, rate limiting, safe configuration,
structured logging, API documentation, and a test suite built around everything that can go wrong.**

## Setup (Windows, no Docker)
```powershell
copy .env.example .env                       # edit DATABASE_URL etc. (never commit .env)
pip install -r requirements-dev.txt
python -m app.manage check-config            # validates .env, prints it with secrets masked
python -m app.manage create-admin --email you@example.com --name "Your Name"     # hidden password prompt
uvicorn app.main:app                         # API + background worker + migrations
```
Open http://127.0.0.1:8000/docs → `POST /auth/login` → copy `access_token` → **Authorize** → paste. Done.
**Every endpoint except `/health` and `/auth/login` now requires a token.** There are no default accounts.

## Demonstrate it
```powershell
python scripts/demo.py --email you@example.com          # ~45 checks: happy paths AND failures, with PASS/FAIL
python scripts/load_test.py --email you@example.com     # 100 simultaneous buyers, 30 duplicate submissions
pytest -v                                               # the full suite (needs TEST_DATABASE_URL)
```

## Security model
| Area | How it works |
|---|---|
| Passwords | Salted scrypt (stdlib). Min 10 chars, max 128, common/related-to-email passwords refused. Never logged or returned. |
| Sessions | Random opaque tokens (`inv_…`). Only a SHA-256 is stored, so a database leak yields no usable tokens. Expire after `TOKEN_TTL_HOURS`. Revoked instantly on logout, password change, or deactivating the user. |
| Login protection | Per-address throttle (`RATE_LIMIT_LOGIN`) **and** per-account lockout after `MAX_FAILED_LOGINS` wrong passwords for `LOCKOUT_MINUTES`. Every failure (unknown email, wrong password, locked, disabled) gives the same response and takes the same time, so nobody can discover which emails have accounts. |
| Authorization | Roles `viewer` / `staff` / `admin` (table below). Checked on every request against the *current* role, so demotions apply immediately. The last active admin can never be removed. |
| Accountability | Orders, status changes and stock movements record who did them. Logins, failures, lockouts, password and user changes go to `security_events` (`GET /security/events`). |
| Rate limiting | Per address (all routes), per user (reads and writes separately), and strict for login. `429` + `Retry-After`. `X-Forwarded-For` is ignored unless `TRUST_PROXY_HEADERS=1`. |
| Headers & limits | `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Cache-Control: no-store`, CSP on JSON, optional HSTS. Request bodies over `MAX_BODY_BYTES` get 413. CORS is closed unless origins are listed. |
| Errors | One shape for everything, with a `request_id`. 500s never reveal internals; unexpected database failures become clean 503s. |

### Who can do what
| | viewer | staff | admin |
|---|:-:|:-:|:-:|
| Read products, stock, orders, invoices | ✔ | ✔ | ✔ |
| Create products, adjust stock, place / advance / cancel orders | | ✔ | ✔ |
| Users, reports, operations, notifications, audit, security events | | | ✔ |
A test (`test_authorization.py`) fails if any route is missing from this table.

## Safe configuration
Settings come only from environment variables / `.env`, are validated at startup, and problems are all listed at once.
`APP_ENV=production` **refuses to start** with a weak database password or `CORS_ORIGINS=*`, hides `/docs`, and logs JSON.
The startup log shows the configuration with passwords masked. Use `python -m app.manage check-config` any time.

| Variable | Default | Meaning |
|---|---|---|
| `APP_ENV` | development | `development`, `test`, `production` |
| `TOKEN_TTL_HOURS` | 12 | Session lifetime |
| `MAX_FAILED_LOGINS` / `LOCKOUT_MINUTES` | 5 / 15 | Account lockout |
| `RATE_LIMIT_LOGIN` / `_WRITE` / `_READ` / `_IP` | 10 / 120 / 600 / 1200 per minute | See above |
| `CORS_ORIGINS` | (none) | Comma-separated exact origins |
| `DOCS_ENABLED` | on (off in production) | `/docs`, `/redoc`, `/openapi.json` |
| `TRUST_PROXY_HEADERS` | 0 | Trust `X-Forwarded-For` (only behind your own proxy) |
| `ENABLE_HSTS` | 0 | Add HSTS (when served over HTTPS) |
| `MAX_BODY_BYTES` | 1000000 | Request size limit |
| `LOG_FORMAT` / `LOG_LEVEL` | text (json in production) / INFO | Logging |
| `DB_POOL_TIMEOUT` | 10 | Seconds to wait for a database connection before answering 503 |

## Logging
One JSON object per line in production: `ts, level, logger, message, request_id, user_id` plus event fields
(`method, path, status, duration_ms, client_ip` for every request). Passwords, tokens and `Authorization` headers are never
logged (and a redaction filter catches accidents). Send `X-Request-ID` to correlate your own traces; the same id appears in
the response header, in error bodies and in every log line of that request.

## API documentation
Interactive docs at `/docs` (Swagger) and `/redoc`: authentication flow, roles, error format, rate limits, idempotency,
every endpoint tagged with its error responses and an **Authorize** button. Export a static copy:
`python -m app.manage export-openapi > openapi.json`.

## What the tests prove (`pytest -v`)
| Requirement | Files |
|---|---|
| Invalid orders (≈60 malformed bodies, bad JSON, wrong types, huge/odd values, SQL-injection text, Unicode) | `test_invalid_orders.py` |
| Insufficient stock (exact boundaries, all-or-nothing, multi-product, key not burned) | `test_insufficient_stock.py` |
| Concurrent requests (database level and **real simultaneous HTTP** against a live server) | `test_concurrency.py`, `test_http_concurrency.py` |
| Duplicate submissions | `test_idempotency.py`, `test_http_concurrency.py` |
| Database failures (crash mid-order rolls back, stuck lock → 503, killed connections heal, pool exhaustion, unreachable DB / wrong password at startup) | `test_db_failures.py` |
| Email failures (refused connection, rejected credentials, hung mail server, retries, give-up, manual recovery) | `test_email_failures.py`, `test_resilience.py`, `test_worker.py` |
| Authentication, lockout, sessions | `test_auth.py` |
| Authorization (role × route table, route-coverage guard, attribution) | `test_authorization.py` |
| Rate limiting | `test_rate_limit.py`, `test_security.py` |
| Safe config, headers, CORS, request ids, log hygiene | `test_settings.py`, `test_http_basics.py`, `test_logging.py` |

## Known limits (be upfront about these when demonstrating)
* The rate limiter keeps counts **per process**. With several server processes each has its own counters (login lockout is in the database, so it is exact). Use a shared store (e.g. Redis) if you need exact global limits.
* The body-size limit uses the `Content-Length` header; a chunked upload without one is only bounded by the field limits.
* There is no TLS in the app: run it behind an HTTPS reverse proxy and set `ENABLE_HSTS=1`.
* No two-factor login and no self-service "forgot password" yet (admins reset passwords; `python -m app.manage reset-password` covers a locked-out admin). An email-OTP reset fits naturally on the existing email queue.
* Security events are written best-effort: a failure to write one is logged but does not fail the request.
* A browser cannot open an invoice link directly (it cannot send the bearer header); download invoices through the API client or Swagger.

## Earlier phases (unchanged)
Order lifecycle `confirmed → processing → shipped → completed` (cancel until shipped, stock restored atomically), PDF invoices,
`Idempotency-Key` on `POST /orders`, row-level locking, outbox email worker with retries, daily report, housekeeping,
`/ops/status`, `/notifications`.

## Upgrading from Phase 4
`pip install -r requirements.txt` (no new packages). Migration `004_security.sql` runs automatically. **Create an admin first**
(`python -m app.manage create-admin …`) or you cannot sign in. Update scripts/clients to send `Authorization: Bearer <token>`.
