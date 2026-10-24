-- Phase 4: durable background jobs. Everything the worker does is recorded in the database.

-- New notification type for scheduled reports.
DO $$
DECLARE c RECORD;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint WHERE conrelid = 'notifications'::regclass AND contype = 'c' LOOP
        EXECUTE 'ALTER TABLE notifications DROP CONSTRAINT ' || quote_ident(c.conname);
    END LOOP;
END
$$;

ALTER TABLE notifications
    ADD CONSTRAINT notifications_type_valid CHECK (type IN
        ('order_confirmed', 'order_shipped', 'order_completed', 'order_cancelled', 'low_stock', 'daily_report')),
    ADD CONSTRAINT notifications_status_valid CHECK (status IN ('pending', 'sent', 'failed', 'skipped'));

-- One row per delivery attempt: the full failure history, not just the latest error.
CREATE TABLE notification_attempts (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    notification_id  BIGINT      NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
    attempt          INTEGER     NOT NULL,
    outcome          TEXT        NOT NULL CHECK (outcome IN ('sent', 'failed', 'skipped')),
    permanent        BOOLEAN     NOT NULL DEFAULT false,   -- true = retrying cannot help, gave up at once
    error            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX notification_attempts_idx ON notification_attempts (notification_id, id);

-- Stored reports (also viewable through the API).
CREATE TABLE reports (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind          TEXT        NOT NULL,
    period_start  TIMESTAMPTZ NOT NULL,
    period_end    TIMESTAMPTZ NOT NULL,
    data          JSONB       NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (kind, period_end)
);

-- Recurring jobs. next_run_at is advanced in the SAME transaction that enqueues the job's output,
-- so each scheduled run happens exactly once even with several workers.
CREATE TABLE schedules (
    id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name                  TEXT        NOT NULL UNIQUE,
    job_type              TEXT        NOT NULL CHECK (job_type IN ('daily_report', 'housekeeping')),
    run_at                TIME        NOT NULL,
    timezone              TEXT        NOT NULL,
    enabled               BOOLEAN     NOT NULL DEFAULT true,
    next_run_at           TIMESTAMPTZ NOT NULL,
    last_run_at           TIMESTAMPTZ,
    last_status           TEXT,
    last_error            TEXT,
    consecutive_failures  INTEGER     NOT NULL DEFAULT 0,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX schedules_due_idx ON schedules (next_run_at) WHERE enabled;

-- History of every scheduled run, successful or not.
CREATE TABLE schedule_runs (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    schedule_name  TEXT        NOT NULL,
    scheduled_for  TIMESTAMPTZ NOT NULL,
    status         TEXT        NOT NULL CHECK (status IN ('ok', 'failed')),
    detail         TEXT,
    error          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX schedule_runs_recent_idx ON schedule_runs (id DESC);

-- Lets the API tell whether a worker is alive.
CREATE TABLE worker_heartbeats (
    worker_id   TEXT        PRIMARY KEY,
    info        TEXT,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now()
);
