-- =====================================================================
-- Flight Management System — Dual-Writer Schema (FastAPI + n8n)
-- Target: Supabase Postgres
-- Migration: 0001_init.sql
--
-- OWNERSHIP LEGEND (see COMMENT ON TABLE for authoritative per-table rule):
--   [FastAPI]  sole writer, live/transactional
--   [n8n]      sole writer, scheduled/background
--   [Both]     shared writers — protected by row-locking + invariants below
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()

-- ---------------------------------------------------------------------
-- ENUM TYPES
-- ---------------------------------------------------------------------

CREATE TYPE seat_class_name AS ENUM ('first', 'business', 'economy');

CREATE TYPE flight_status AS ENUM ('scheduled', 'delayed', 'cancelled', 'departed');

CREATE TYPE booking_status AS ENUM ('held', 'confirmed', 'cancelled', 'refunded', 'waitlisted');

CREATE TYPE fare_type AS ENUM ('basic_economy', 'flexible', 'business_flex', 'first_flex');

CREATE TYPE waitlist_status AS ENUM ('waiting', 'notified', 'promoted', 'expired', 'cancelled');

CREATE TYPE admin_role AS ENUM ('super_admin', 'ops_agent');

-- ---------------------------------------------------------------------
-- ADMINS  (referenced by audit log; permission-tier requirement)
-- ---------------------------------------------------------------------

CREATE TABLE admins (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT NOT NULL UNIQUE,
    role          admin_role NOT NULL DEFAULT 'ops_agent',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE admins IS 'OWNER: FastAPI. n8n never writes here.';

-- ---------------------------------------------------------------------
-- FLIGHTS
-- ---------------------------------------------------------------------

CREATE TABLE flights (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    flight_number    TEXT NOT NULL,
    origin           TEXT NOT NULL,
    destination      TEXT NOT NULL,
    departure_at     TIMESTAMPTZ NOT NULL,
    arrival_at       TIMESTAMPTZ NOT NULL,
    total_seats      INTEGER NOT NULL CHECK (total_seats > 0),
    status           flight_status NOT NULL DEFAULT 'scheduled',
    created_by       UUID REFERENCES admins(id),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    CHECK (arrival_at > departure_at)
);

-- Duplicate flight-number detection: same flight number cannot depart
-- on the same route on the same calendar day.
CREATE UNIQUE INDEX uq_flight_number_route_day
    ON flights (flight_number, origin, destination, (CAST(departure_at AT TIME ZONE 'UTC' AS DATE)))
    WHERE status <> 'cancelled';

CREATE INDEX idx_flights_route_date ON flights (origin, destination, departure_at);

COMMENT ON TABLE flights IS
    'OWNER: FastAPI (sole writer). All create/edit/cancel operations go through the FastAPI admin routes so inventory changes stay transactionally consistent with seat_classes. n8n only ever SELECTs from this table (e.g. to suppress reminders for cancelled flights).';

-- ---------------------------------------------------------------------
-- SEAT CLASSES  (per-flight inventory buckets — the overselling guard)
-- ---------------------------------------------------------------------

CREATE TABLE seat_classes (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    flight_id        UUID NOT NULL REFERENCES flights(id) ON DELETE CASCADE,
    class_name       seat_class_name NOT NULL,
    allocated_seats  INTEGER NOT NULL CHECK (allocated_seats >= 0),
    booked_seats     INTEGER NOT NULL DEFAULT 0 CHECK (booked_seats >= 0),
    held_seats       INTEGER NOT NULL DEFAULT 0 CHECK (held_seats >= 0),
    overbook_buffer  INTEGER NOT NULL DEFAULT 0 CHECK (overbook_buffer >= 0),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (flight_id, class_name),

    -- Core anti-overselling invariant: booked + held can never exceed
    -- allocation + explicit overbook buffer for that class.
    CHECK (booked_seats + held_seats <= allocated_seats + overbook_buffer)
);

CREATE INDEX idx_seat_classes_flight ON seat_classes (flight_id);

COMMENT ON TABLE seat_classes IS
    'OWNER: FastAPI (sole writer). Every UPDATE to booked_seats/held_seats must be an atomic conditional UPDATE (see hold_seats()/confirm_booking() below) so two concurrent bookings cannot both claim the last seat. n8n reads this table for reporting/waitlist promotion eligibility checks but never writes it directly — promotion still goes through the FastAPI-owned atomic functions.';

-- Trigger: sum(allocated_seats) for a flight must equal flights.total_seats.
-- A CHECK constraint cannot span rows/tables, so this is enforced with a
-- constraint trigger that fires after any INSERT/UPDATE/DELETE on the
-- flight's seat_classes rows.
CREATE OR REPLACE FUNCTION fn_validate_seat_allocation_sum()
RETURNS TRIGGER AS $$
DECLARE
    v_flight_id UUID;
    v_total INTEGER;
    v_sum   INTEGER;
BEGIN
    v_flight_id := COALESCE(NEW.flight_id, OLD.flight_id);

    SELECT total_seats INTO v_total FROM flights WHERE id = v_flight_id;
    SELECT COALESCE(SUM(allocated_seats), 0) INTO v_sum
        FROM seat_classes WHERE flight_id = v_flight_id;

    IF v_sum <> v_total THEN
        RAISE EXCEPTION
            'seat_classes allocation sum (%) does not match flights.total_seats (%) for flight %',
            v_sum, v_total, v_flight_id;
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_validate_seat_allocation_sum
    AFTER INSERT OR UPDATE OR DELETE ON seat_classes
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION fn_validate_seat_allocation_sum();

-- Trigger: cannot shrink a class allocation below its already-booked count.
CREATE OR REPLACE FUNCTION fn_prevent_shrink_below_booked()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.allocated_seats < NEW.booked_seats THEN
        RAISE EXCEPTION
            'cannot set allocated_seats (%) below booked_seats (%) for seat_class %',
            NEW.allocated_seats, NEW.booked_seats, NEW.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_prevent_shrink_below_booked
    BEFORE UPDATE OF allocated_seats ON seat_classes
    FOR EACH ROW EXECUTE FUNCTION fn_prevent_shrink_below_booked();

-- ---------------------------------------------------------------------
-- BOOKINGS
-- ---------------------------------------------------------------------

CREATE TABLE bookings (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    flight_id         UUID NOT NULL REFERENCES flights(id),
    seat_class_id     UUID NOT NULL REFERENCES seat_classes(id),
    passenger_name    TEXT NOT NULL,
    passenger_email   TEXT NOT NULL,
    num_seats         INTEGER NOT NULL CHECK (num_seats > 0),
    fare_type         fare_type NOT NULL,
    price_total       NUMERIC(12, 2) NOT NULL CHECK (price_total >= 0),
    currency          TEXT NOT NULL DEFAULT 'USD',
    status            booking_status NOT NULL DEFAULT 'held',
    idempotency_key   TEXT NOT NULL,
    hold_expires_at   TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- A booking that is still "held" must carry an expiry.
    CHECK (status <> 'held' OR hold_expires_at IS NOT NULL)
);

-- Idempotency: retried requests with the same key must resolve to the
-- same booking, not create a duplicate.
CREATE UNIQUE INDEX uq_bookings_idempotency_key ON bookings (idempotency_key);

CREATE INDEX idx_bookings_flight_class ON bookings (flight_id, seat_class_id);
CREATE INDEX idx_bookings_hold_expiry
    ON bookings (hold_expires_at) WHERE status = 'held';

COMMENT ON TABLE bookings IS
    'OWNER: FastAPI (sole writer). All inserts/updates happen inside the same transaction as the seat_classes decrement in hold_seats()/confirm_booking(). n8n only reads this table (refund-escalation checks, reporting, fraud scoring) and never writes booking rows.';

-- ---------------------------------------------------------------------
-- WAITLIST
-- ---------------------------------------------------------------------

CREATE TABLE waitlist (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    flight_id             UUID NOT NULL REFERENCES flights(id),
    seat_class_id         UUID NOT NULL REFERENCES seat_classes(id),
    passenger_name        TEXT NOT NULL,
    passenger_email       TEXT NOT NULL,
    loyalty_tier          INTEGER NOT NULL DEFAULT 0,
    status                waitlist_status NOT NULL DEFAULT 'waiting',
    promotion_notified_at TIMESTAMPTZ,
    promotion_expires_at  TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_waitlist_promotion_order
    ON waitlist (flight_id, seat_class_id, loyalty_tier DESC, created_at ASC)
    WHERE status = 'waiting';

COMMENT ON TABLE waitlist IS
    'OWNER: Both. FastAPI is the sole writer of INSERTs (a passenger joins a waitlist as part of a live booking attempt on a full class). n8n is the sole writer of promotion UPDATEs (status -> notified/promoted/expired), executed only through promote_next_waitlisted() below, which takes the same row lock as any concurrent FastAPI gate-agent action on that seat_class.';

-- ---------------------------------------------------------------------
-- ADMIN AUDIT LOG
-- ---------------------------------------------------------------------

CREATE TABLE admin_audit_log (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    admin_id      UUID REFERENCES admins(id),
    flight_id     UUID REFERENCES flights(id),
    action        TEXT NOT NULL,
    old_value     JSONB,
    new_value     JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE admin_audit_log IS
    'OWNER: FastAPI (sole writer). Every admin mutation to flights/seat_classes writes a row here in the same transaction, so it doubles as the regulator-facing audit trail referenced in the approval/autonomy-boundary requirement.';

-- ---------------------------------------------------------------------
-- updated_at maintenance (generic trigger, applied per table)
-- ---------------------------------------------------------------------

CREATE OR REPLACE FUNCTION fn_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_touch_flights       BEFORE UPDATE ON flights       FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();
CREATE TRIGGER trg_touch_seat_classes  BEFORE UPDATE ON seat_classes  FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();
CREATE TRIGGER trg_touch_bookings      BEFORE UPDATE ON bookings      FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();
CREATE TRIGGER trg_touch_waitlist      BEFORE UPDATE ON waitlist      FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();

-- =====================================================================
-- ROW-LOCKING FUNCTIONS
-- These are the only sanctioned mutation paths for seat_classes/waitlist.
-- FastAPI calls hold_seats()/confirm_booking()/release_expired_holds();
-- n8n calls promote_next_waitlisted(). Both rely on SELECT ... FOR UPDATE
-- (with SKIP LOCKED where concurrent callers can legitimately try
-- different rows) so a gate-agent action and an n8n job can never both
-- assign the same freed seat twice.
-- =====================================================================

-- 1. Atomic seat hold (checkout start). Returns TRUE if the hold succeeded.
CREATE OR REPLACE FUNCTION hold_seats(
    p_seat_class_id UUID,
    p_num_seats     INTEGER
) RETURNS BOOLEAN AS $$
DECLARE
    v_ok BOOLEAN;
BEGIN
    -- Lock this specific seat_class row so no concurrent booking can
    -- read a stale count between the check and the decrement.
    PERFORM 1 FROM seat_classes
        WHERE id = p_seat_class_id
        FOR UPDATE;

    UPDATE seat_classes
       SET held_seats = held_seats + p_num_seats
     WHERE id = p_seat_class_id
       AND booked_seats + held_seats + p_num_seats <= allocated_seats + overbook_buffer
     RETURNING TRUE INTO v_ok;

    RETURN COALESCE(v_ok, FALSE);
END;
$$ LANGUAGE plpgsql;

-- 2. Convert a held booking into a confirmed one (payment succeeded).
CREATE OR REPLACE FUNCTION confirm_booking(
    p_booking_id UUID
) RETURNS BOOLEAN AS $$
DECLARE
    v_seat_class_id UUID;
    v_num_seats     INTEGER;
BEGIN
    SELECT seat_class_id, num_seats INTO v_seat_class_id, v_num_seats
        FROM bookings
        WHERE id = p_booking_id AND status = 'held'
        FOR UPDATE;

    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    UPDATE seat_classes
       SET held_seats   = held_seats - v_num_seats,
           booked_seats = booked_seats + v_num_seats
     WHERE id = v_seat_class_id;

    UPDATE bookings
       SET status = 'confirmed', hold_expires_at = NULL
     WHERE id = p_booking_id;

    RETURN TRUE;
END;
$$ LANGUAGE plpgsql;

-- 3. Release expired holds (called by a FastAPI background task on a
--    short interval; kept as a FastAPI-owned function since it mutates
--    the same booking/seat_classes rows as the live booking path).
CREATE OR REPLACE FUNCTION release_expired_holds() RETURNS INTEGER AS $$
DECLARE
    v_count INTEGER := 0;
    v_row   RECORD;
BEGIN
    FOR v_row IN
        SELECT id, seat_class_id, num_seats
        FROM bookings
        WHERE status = 'held' AND hold_expires_at < now()
        FOR UPDATE SKIP LOCKED
    LOOP
        UPDATE seat_classes
           SET held_seats = held_seats - v_row.num_seats
         WHERE id = v_row.seat_class_id;

        UPDATE bookings SET status = 'cancelled' WHERE id = v_row.id;

        v_count := v_count + 1;
    END LOOP;

    RETURN v_count;
END;
$$ LANGUAGE plpgsql;

-- 4. n8n's sole write path into waitlist/seat_classes: promote the next
--    eligible waitlisted passenger for a freed seat. SKIP LOCKED lets a
--    concurrent FastAPI gate-agent action on the same flight proceed
--    against a different waitlist row instead of blocking, while FOR
--    UPDATE on the seat_classes row still serializes against any writer
--    touching that exact class.
CREATE OR REPLACE FUNCTION promote_next_waitlisted(
    p_seat_class_id UUID
) RETURNS UUID AS $$
DECLARE
    v_waitlist_id UUID;
    v_ok          BOOLEAN;
BEGIN
    PERFORM 1 FROM seat_classes WHERE id = p_seat_class_id FOR UPDATE;

    SELECT id INTO v_waitlist_id
        FROM waitlist
        WHERE seat_class_id = p_seat_class_id AND status = 'waiting'
        ORDER BY loyalty_tier DESC, created_at ASC
        FOR UPDATE SKIP LOCKED
        LIMIT 1;

    IF v_waitlist_id IS NULL THEN
        RETURN NULL;
    END IF;

    UPDATE seat_classes
       SET held_seats = held_seats + 1
     WHERE id = p_seat_class_id
       AND booked_seats + held_seats + 1 <= allocated_seats + overbook_buffer
     RETURNING TRUE INTO v_ok;

    IF NOT v_ok THEN
        RETURN NULL;  -- seat was reclaimed elsewhere between checks
    END IF;

    UPDATE waitlist
       SET status = 'notified',
           promotion_notified_at = now(),
           promotion_expires_at = now() + INTERVAL '24 hours'
     WHERE id = v_waitlist_id;

    RETURN v_waitlist_id;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
-- End of migration 0001_init.sql
-- =====================================================================