-- Phase 2 schema. Money is integer cents. Every table has a primary key; every
-- rule that protects inventory is ALSO enforced by the database (CHECKs, FKs, triggers),
-- so a bug in application code cannot silently corrupt stock.

CREATE TABLE products (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku                 TEXT        NOT NULL CHECK (length(sku)  BETWEEN 1 AND 64),
    name                TEXT        NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    price_cents         INTEGER     NOT NULL CHECK (price_cents > 0),
    stock               INTEGER     NOT NULL DEFAULT 0 CHECK (stock >= 0),      -- can never go negative
    low_stock_threshold INTEGER     NOT NULL DEFAULT 5 CHECK (low_stock_threshold >= 0),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX products_sku_lower_uq ON products (lower(sku));

CREATE TABLE orders (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    status       TEXT        NOT NULL CHECK (status IN ('confirmed', 'cancelled')),
    total_cents  BIGINT      NOT NULL CHECK (total_cents >= 0),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE order_items (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id          BIGINT  NOT NULL REFERENCES orders(id)   ON DELETE RESTRICT,
    product_id        BIGINT  NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    sku               TEXT    NOT NULL,   -- snapshot at purchase time
    name              TEXT    NOT NULL,   -- snapshot at purchase time
    unit_price_cents  INTEGER NOT NULL CHECK (unit_price_cents > 0),   -- snapshot at purchase time
    quantity          INTEGER NOT NULL CHECK (quantity > 0),
    line_total_cents  BIGINT  NOT NULL CHECK (line_total_cents > 0),
    UNIQUE (order_id, product_id)
);
CREATE INDEX order_items_product_idx ON order_items (product_id);

-- Append-only ledger of every stock change. Invariant: products.stock = SUM(change).
CREATE TABLE inventory_movements (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id   BIGINT      NOT NULL REFERENCES products(id),
    change       INTEGER     NOT NULL CHECK (change <> 0),
    stock_after  INTEGER     NOT NULL CHECK (stock_after >= 0),
    reason       TEXT        NOT NULL CHECK (reason IN ('initial_stock', 'order', 'manual_adjustment', 'manual_set')),
    order_id     BIGINT      REFERENCES orders(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((reason = 'order') = (order_id IS NOT NULL))
);
CREATE INDEX inventory_movements_product_idx ON inventory_movements (product_id, id);

CREATE FUNCTION forbid_modification() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'inventory_movements is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER inventory_movements_append_only
    BEFORE UPDATE OR DELETE ON inventory_movements
    FOR EACH ROW EXECUTE FUNCTION forbid_modification();

-- Notifications are written in the SAME transaction as the event that causes them
-- (transactional outbox), so an order can never exist without its notification, or vice versa.
-- A separate worker (later phase) delivers 'pending' rows.
CREATE TABLE notifications (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    type        TEXT        NOT NULL CHECK (type IN ('order_confirmed', 'low_stock')),
    payload     JSONB       NOT NULL,
    status      TEXT        NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
    attempts    INTEGER     NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at     TIMESTAMPTZ
);
CREATE INDEX notifications_pending_idx ON notifications (id) WHERE status = 'pending';

-- Idempotency: the key is claimed inside the order's own transaction. If the order fails
-- and rolls back, the claim rolls back too, so a failed attempt never "poisons" the key.
CREATE TABLE idempotency_keys (
    key           TEXT        PRIMARY KEY CHECK (length(key) BETWEEN 1 AND 255),
    request_hash  TEXT        NOT NULL,
    order_id      BIGINT      REFERENCES orders(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
