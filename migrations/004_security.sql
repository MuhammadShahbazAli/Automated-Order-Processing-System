-- Phase 5: users, sessions, security events, and "who did it" columns.

CREATE TABLE users (
    id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email                 TEXT        NOT NULL CHECK (length(email) BETWEEN 3 AND 254),
    full_name             TEXT        NOT NULL CHECK (length(full_name) BETWEEN 1 AND 200),
    password_hash         TEXT        NOT NULL,
    role                  TEXT        NOT NULL CHECK (role IN ('viewer', 'staff', 'admin')),
    is_active             BOOLEAN     NOT NULL DEFAULT true,
    failed_logins         INTEGER     NOT NULL DEFAULT 0,
    locked_until          TIMESTAMPTZ,
    password_changed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at         TIMESTAMPTZ
);
CREATE UNIQUE INDEX users_email_lower_uq ON users (lower(email));

-- Only a SHA-256 hash of each token is stored: a database leak does not leak usable tokens.
CREATE TABLE sessions (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    token_hash     TEXT        NOT NULL UNIQUE,
    user_id        BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL,
    last_used_at   TIMESTAMPTZ,
    revoked_at     TIMESTAMPTZ,
    ip             TEXT,
    user_agent     TEXT
);
CREATE INDEX sessions_user_idx ON sessions (user_id);

CREATE TABLE security_events (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event       TEXT        NOT NULL,
    user_id     BIGINT,
    email       TEXT,
    ip          TEXT,
    detail      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX security_events_recent_idx ON security_events (id DESC);

-- Who did what (NULL for work done by the system itself or before accounts existed).
ALTER TABLE orders               ADD COLUMN created_by BIGINT REFERENCES users(id);
ALTER TABLE order_status_history ADD COLUMN actor_id   BIGINT REFERENCES users(id);
ALTER TABLE inventory_movements  ADD COLUMN actor_id   BIGINT REFERENCES users(id);
