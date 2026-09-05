"""
Admin & flight-management endpoints.

Ownership per the schema comments in 0001_init.sql: FastAPI is the sole
writer of `flights`, `seat_classes`, and `admin_audit_log`. Every mutation
here happens inside one asyncpg transaction together with its audit_log
row, so an audit entry can never exist without the change it describes
(or vice versa).

Auth note: the schema's `admins` table and `admin_role` enum imply a real
auth layer (e.g. Supabase Auth / JWT) that is out of scope for this chat.
`get_current_admin_id` below is a placeholder dependency — it trusts an
`X-Admin-Id` header — and is the one thing in this file you should not
ship to production as-is; swap its body for real token verification.
"""

from __future__ import annotations

import json
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.database import get_pool
from app.models.admin import (
    FlightCancelRequest,
    FlightCreateRequest,
    FlightResponse,
    FlightUpdateRequest,
    SeatClassResponse,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# auth placeholder
# ---------------------------------------------------------------------------

async def get_current_admin_id(
    x_admin_id: UUID | None = Header(default=None, alias="X-Admin-Id"),
) -> UUID:
    """Placeholder admin-identity dependency.

    Replace with real auth (verify a Supabase JWT / session, look up the
    admins row, check admin_role for the tiered-permission endpoints).
    This only checks that *some* admin id was supplied and that it exists
    in `admins`, it does not check role tier.
    """
    if x_admin_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Admin-Id header is required",
        )
    return x_admin_id


async def _require_admin_exists(conn: asyncpg.Connection, admin_id: UUID) -> None:
    exists = await conn.fetchval("SELECT 1 FROM admins WHERE id = $1", admin_id)
    if not exists:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown admin id")


# ---------------------------------------------------------------------------
# shared row -> response assembly
# ---------------------------------------------------------------------------

async def _load_flight_response(conn: asyncpg.Connection, flight_id: UUID) -> FlightResponse:
    flight_row = await conn.fetchrow("SELECT * FROM flights WHERE id = $1", flight_id)
    if flight_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flight not found")

    seat_class_rows = await conn.fetch(
        "SELECT * FROM seat_classes WHERE flight_id = $1 ORDER BY class_name", flight_id
    )
    seat_classes = [SeatClassResponse(**dict(r)) for r in seat_class_rows]
    return FlightResponse(**dict(flight_row), seat_classes=seat_classes)


async def _write_audit_log(
    conn: asyncpg.Connection,
    *,
    admin_id: UUID,
    flight_id: UUID,
    action: str,
    old_value: dict | None,
    new_value: dict | None,
) -> None:
    await conn.execute(
        """
        INSERT INTO admin_audit_log (admin_id, flight_id, action, old_value, new_value)
        VALUES ($1, $2, $3, $4::jsonb, $5::jsonb)
        """,
        admin_id,
        flight_id,
        action,
        json.dumps(old_value, default=str) if old_value is not None else None,
        json.dumps(new_value, default=str) if new_value is not None else None,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/admin/flights
# ---------------------------------------------------------------------------

@router.post(
    "/flights",
    response_model=FlightResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_flight(
    payload: FlightCreateRequest,
    admin_id: UUID = Depends(get_current_admin_id),
    pool: asyncpg.Pool = Depends(get_pool),
) -> FlightResponse:
    # Pydantic already checked allocated_seats sums to total_seats and that
    # arrival_at > departure_at; Postgres re-checks both (CHECK constraint +
    # the deferred trg_validate_seat_allocation_sum trigger) so a direct-SQL
    # caller (n8n, psql) can't bypass the invariant either.
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                flight_row = await conn.fetchrow(
                    """
                    INSERT INTO flights
                        (flight_number, origin, destination, departure_at,
                         arrival_at, total_seats, created_by)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    RETURNING *
                    """,
                    payload.flight_number,
                    payload.origin.upper(),
                    payload.destination.upper(),
                    payload.departure_at,
                    payload.arrival_at,
                    payload.total_seats,
                    admin_id,
                )
            except asyncpg.UniqueViolationError:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="A flight with this number/route already departs on that day",
                )
            except asyncpg.CheckViolationError as exc:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

            flight_id = flight_row["id"]

            for sc in payload.seat_classes:
                await conn.execute(
                    """
                    INSERT INTO seat_classes
                        (flight_id, class_name, allocated_seats, overbook_buffer)
                    VALUES ($1, $2::seat_class_name, $3, $4)
                    """,
                    flight_id,
                    sc.class_name.value,
                    sc.allocated_seats,
                    sc.overbook_buffer,
                )

            await _write_audit_log(
                conn,
                admin_id=admin_id,
                flight_id=flight_id,
                action="create_flight",
                old_value=None,
                new_value=payload.model_dump(mode="json"),
            )

            # trg_validate_seat_allocation_sum is DEFERRABLE INITIALLY DEFERRED,
            # so it fires at COMMIT time (end of this `async with conn.transaction()`
            # block) rather than after each individual INSERT above.
            return await _load_flight_response(conn, flight_id)


# ---------------------------------------------------------------------------
# PATCH /api/v1/admin/flights/{id}
# ---------------------------------------------------------------------------

@router.patch("/flights/{flight_id}", response_model=FlightResponse)
async def update_flight(
    flight_id: UUID,
    payload: FlightUpdateRequest,
    pool: asyncpg.Pool = Depends(get_pool),
) -> FlightResponse:
    """Edit schedule/route/status and/or patch individual seat-class
    allocations. Any attempt to shrink a class below its booked_seats is
    rejected by trg_prevent_shrink_below_booked; any attempt to leave the
    allocation sum mismatched with total_seats is rejected by
    trg_validate_seat_allocation_sum at commit time. Both surface here as
    HTTP 409 rather than a raw asyncpg traceback.

    NOTE: cascading rebooking/refund effects on existing bookings from a
    schedule change are a separate, business-rule-heavy flow (see the
    capstone's "Changes, Cancellations & Refunds" domain) and are
    intentionally out of scope for this endpoint — it only updates the
    flight/seat_classes rows themselves.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            old_row = await conn.fetchrow("SELECT * FROM flights WHERE id = $1 FOR UPDATE", flight_id)
            if old_row is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flight not found")

            old_seat_rows = await conn.fetch(
                "SELECT * FROM seat_classes WHERE flight_id = $1", flight_id
            )

            set_clauses: list[str] = []
            values: list = []
            param_idx = 1

            def add_field(column: str, value, cast: str | None = None) -> None:
                nonlocal param_idx
                placeholder = f"${param_idx}" + (f"::{cast}" if cast else "")
                set_clauses.append(f"{column} = {placeholder}")
                values.append(value)
                param_idx += 1

            if payload.origin is not None:
                add_field("origin", payload.origin.upper())
            if payload.destination is not None:
                add_field("destination", payload.destination.upper())
            if payload.departure_at is not None:
                add_field("departure_at", payload.departure_at)
            if payload.arrival_at is not None:
                add_field("arrival_at", payload.arrival_at)
            if payload.status is not None:
                add_field("status", payload.status.value, cast="flight_status")
            if payload.total_seats is not None:
                add_field("total_seats", payload.total_seats)

            try:
                if set_clauses:
                    values.append(flight_id)
                    query = (
                        f"UPDATE flights SET {', '.join(set_clauses)} "
                        f"WHERE id = ${param_idx} RETURNING *"
                    )
                    await conn.fetchrow(query, *values)

                if payload.seat_classes:
                    for patch in payload.seat_classes:
                        existing = await conn.fetchrow(
                            """
                            SELECT id FROM seat_classes
                            WHERE flight_id = $1 AND class_name = $2::seat_class_name
                            FOR UPDATE
                            """,
                            flight_id,
                            patch.class_name.value,
                        )
                        if existing is None:
                            raise HTTPException(
                                status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"No seat_class '{patch.class_name.value}' on this flight",
                            )

                        patch_clauses: list[str] = []
                        patch_values: list = []
                        p_idx = 1
                        if patch.allocated_seats is not None:
                            patch_clauses.append(f"allocated_seats = ${p_idx}")
                            patch_values.append(patch.allocated_seats)
                            p_idx += 1
                        if patch.overbook_buffer is not None:
                            patch_clauses.append(f"overbook_buffer = ${p_idx}")
                            patch_values.append(patch.overbook_buffer)
                            p_idx += 1
                        if not patch_clauses:
                            continue

                        patch_values.append(existing["id"])
                        await conn.execute(
                            f"UPDATE seat_classes SET {', '.join(patch_clauses)} "
                            f"WHERE id = ${p_idx}",
                            *patch_values,
                        )
            except asyncpg.RaiseError as exc:
                # trg_prevent_shrink_below_booked / trg_validate_seat_allocation_sum
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
            except asyncpg.CheckViolationError as exc:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

            await _write_audit_log(
                conn,
                admin_id=payload.updated_by,
                flight_id=flight_id,
                action="update_flight",
                old_value={
                    "flight": dict(old_row),
                    "seat_classes": [dict(r) for r in old_seat_rows],
                },
                new_value=payload.model_dump(mode="json", exclude={"updated_by"}),
            )

            # trigger fires at commit (end of this block) — a mismatched
            # allocation sum raised here would roll back the whole PATCH.
            return await _load_flight_response(conn, flight_id)


# ---------------------------------------------------------------------------
# POST /api/v1/admin/flights/{id}/cancel
# ---------------------------------------------------------------------------

@router.post("/flights/{flight_id}/cancel", response_model=FlightResponse)
async def cancel_flight(
    flight_id: UUID,
    payload: FlightCancelRequest,
    pool: asyncpg.Pool = Depends(get_pool),
) -> FlightResponse:
    """Marks the flight cancelled and audit-logs it. Downstream refund/
    rebooking fan-out (bookings -> refunded/cancelled, waitlist -> cancelled,
    customer emails) belongs to the Changes/Cancellations flow and is not
    duplicated here — this endpoint's contract is just the flights row +
    audit trail so that flow has a reliable trigger to react to."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            old_row = await conn.fetchrow(
                "SELECT * FROM flights WHERE id = $1 FOR UPDATE", flight_id
            )
            if old_row is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flight not found")
            if old_row["status"] == "cancelled":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="Flight is already cancelled"
                )

            await conn.execute(
                "UPDATE flights SET status = 'cancelled'::flight_status WHERE id = $1",
                flight_id,
            )

            await _write_audit_log(
                conn,
                admin_id=payload.cancelled_by,
                flight_id=flight_id,
                action="cancel_flight",
                old_value={"status": old_row["status"]},
                new_value={"status": "cancelled", "reason": payload.reason},
            )

            return await _load_flight_response(conn, flight_id)


# ---------------------------------------------------------------------------
# GET /api/v1/admin/flights/{id}/audit-log  (read-only convenience endpoint)
# ---------------------------------------------------------------------------

@router.get("/flights/{flight_id}/audit-log")
async def get_flight_audit_log(
    flight_id: UUID,
    pool: asyncpg.Pool = Depends(get_pool),
):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM admin_audit_log WHERE flight_id = $1 ORDER BY created_at DESC",
            flight_id,
        )
    return [dict(r) for r in rows]