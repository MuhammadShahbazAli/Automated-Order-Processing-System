-- Phase 3: customer details, order lifecycle, status history, email-ready notifications.
-- The old CHECK constraints are looked up by table (not by guessed name) and replaced with named ones.

-- ---------- orders ----------
ALTER TABLE orders
    ADD COLUMN customer_name   TEXT,
    ADD COLUMN customer_email  TEXT,
    ADD COLUMN customer_phone  TEXT,
    ADD COLUMN shipping_address TEXT,
    ADD COLUMN updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN cancelled_at    TIMESTAMPTZ,
    ADD COLUMN cancel_reason   TEXT;
UPDATE orders SET updated_at = created_at;

DO $$
DECLARE c RECORD;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint WHERE conrelid = 'orders'::regclass AND contype = 'c' LOOP
        EXECUTE 'ALTER TABLE orders DROP CONSTRAINT ' || quote_ident(c.conname);
    END LOOP;
END
$$;

ALTER TABLE orders
    ADD CONSTRAINT orders_total_nonneg CHECK (total_cents >= 0),
    ADD CONSTRAINT orders_status_valid CHECK (status IN ('confirmed', 'processing', 'shipped', 'completed', 'cancelled')),
    ADD CONSTRAINT orders_cancel_consistent CHECK ((status = 'cancelled') = (cancelled_at IS NOT NULL));

CREATE INDEX orders_status_idx ON orders (status, id DESC);

-- ---------- status history (who/what/when for every transition) ----------
CREATE TABLE order_status_history (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id     BIGINT      NOT NULL REFERENCES orders(id),
    from_status  TEXT,
    to_status    TEXT        NOT NULL,
    note         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX order_status_history_order_idx ON order_status_history (order_id, id);

INSERT INTO order_status_history (order_id, from_status, to_status, note, created_at)
SELECT id, NULL, status, 'Created before status history existed', created_at FROM orders;

-- ---------- inventory ledger: cancellations put stock back ----------
DO $$
DECLARE c RECORD;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint WHERE conrelid = 'inventory_movements'::regclass AND contype = 'c' LOOP
        EXECUTE 'ALTER TABLE inventory_movements DROP CONSTRAINT ' || quote_ident(c.conname);
    END LOOP;
END
$$;

ALTER TABLE inventory_movements
    ADD CONSTRAINT movements_change_nonzero CHECK (change <> 0),
    ADD CONSTRAINT movements_stock_after_nonneg CHECK (stock_after >= 0),
    ADD CONSTRAINT movements_reason_valid CHECK (reason IN
        ('initial_stock', 'order', 'order_cancelled', 'manual_adjustment', 'manual_set')),
    ADD CONSTRAINT movements_order_ref CHECK ((reason IN ('order', 'order_cancelled')) = (order_id IS NOT NULL));

-- ---------- notifications: email delivery with retries ----------
ALTER TABLE notifications
    ADD COLUMN recipient        TEXT,
    ADD COLUMN last_error       TEXT,
    ADD COLUMN next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now();

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
        ('order_confirmed', 'order_shipped', 'order_completed', 'order_cancelled', 'low_stock')),
    ADD CONSTRAINT notifications_status_valid CHECK (status IN ('pending', 'sent', 'failed', 'skipped'));
