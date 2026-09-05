"""
Live, transactional booking endpoints.

Ownership per 0001_init.sql: FastAPI is the sole writer of `bookings` and
the sole writer of *INSERTs* into `waitlist` (n8n owns the promotion
UPDATEs via promote_next_waitlisted(), called from its own scheduled job —
not from this service).

Seat-count mutation rule: every place a booking touches booked_seats/
held_seats goes through hold_seats() / confirm_booking(), the two SQL
functions defined alongside the schema. Nothing here does
`UPDATE seat_classes SET held_seats = held_seats + ...` directly — that
would reintroduce the exact race those functions exist to close.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.database import get_pool
from app.models.booking import (
    BookingResponse,
    ConfirmRequest,
    ConfirmResponse,
    FlightSearchResult,
    HoldRequest,
    HoldResponse,
    SeatAvailability,
    WaitlistRequest,
    WaitlistResponse,
)

router = APIRouter(prefix="/api/v1", tags=["booking"])

HOLD_DURATION = timedelta(minutes=15)


# ---------------------------------------------------------------------------
# GET /api/v1/search
# ---------------------------------------------------------------------------

@router.get("/search", response_model=list[FlightSearchResult])
async def search_flights(
    origin: str = Query(min_length=3, max_length=8),
    destination: str = Query(min_length=3, max_length=8),
    departure_date: date = Query(),
    pool: asyncpg.Pool = Depends(get_pool),
) -> list[FlightSearchResult]:
    async with pool.acquire() as conn:
        flight_rows = await conn.fetch(
            """
            SELECT * FROM flights
            WHERE origin = $1
              AND destination = $2
              AND departure_at >= $3::date
              AND departure_at <  ($3::date + INTERVAL '1 day')
              AND status <> 'cancelled'
            ORDER BY departure_at
            """,
            origin.upper(),
            destination.upper(),
            departure_date,
        )

        results: list[FlightSearchResult] = []
        for flight in flight_rows:
            seat_rows = await conn.fetch(
                """
                SELECT id AS seat_class_id, class_name, allocated_seats,
                       overbook_buffer,
                       (allocated_seats + overbook_buffer - booked_seats - held_seats)
                           AS available_seats
                FROM seat_classes
                WHERE flight_id = $1
                ORDER BY class_name
                """,
                flight["id"],
            )
            results.append(
                FlightSearchResult(
                    flight_id=flight["id"],
                    flight_number=flight["flight_number"],
                    origin=flight["origin"],
                    destination=flight["destination"],
                    departure_at=flight["departure_at"],
                    arrival_at=flight["arrival_at"],
                    status=flight["status"],
                    seat_availability=[SeatAvailability(**dict(r)) for r in seat_rows],
                )
            )
        return results


# ---------------------------------------------------------------------------
# POST /api/v1/bookings/hold
# ---------------------------------------------------------------------------

@router.post(
    "/bookings/hold",
    response_model=HoldResponse,
    status_code=status.HTTP_201_CREATED,
)
async def hold_booking(
    payload: HoldRequest,
    pool: asyncpg.Pool = Depends(get_pool),
) -> HoldResponse:
    """
    KNOWN LIMITATION (inherited from the schema's hold_seats() signature):
    hold_seats(seat_class_id, num_seats) is all-or-nothing — it holds every
    requested seat or none of them. Partial-hold-partial-fail for a group
    booking would need a new SQL function, which is out of scope for this
    chat (schema is frozen); for now a group request that can't be fully
    satisfied gets a 409 and the caller can retry with a smaller num_seats
    or use POST /api/v1/waitlist.
    """
    async with pool.acquire() as conn:
        # Idempotency check happens in its own read first so a retried
        # request short-circuits before we touch seat_classes at all.
        existing = await conn.fetchrow(
            "SELECT * FROM bookings WHERE idempotency_key = $1",
            payload.idempotency_key,
        )
        if existing is not None:
            return HoldResponse(
                booking_id=existing["id"],
                status=existing["status"],
                hold_expires_at=existing["hold_expires_at"],
                replayed=True,
            )

        async with conn.transaction():
            # Confirms the seat_class actually belongs to the flight named,
            # and locks nothing itself — hold_seats() takes the row lock.
            seat_class = await conn.fetchrow(
                "SELECT id FROM seat_classes WHERE id = $1 AND flight_id = $2",
                payload.seat_class_id,
                payload.flight_id,
            )
            if seat_class is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="seat_class_id does not belong to flight_id",
                )

            held_ok: bool = await conn.fetchval(
                "SELECT hold_seats($1, $2)", payload.seat_class_id, payload.num_seats
            )
            if not held_ok:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Not enough seats available in this class to hold the full request",
                )

            hold_expires_at = datetime.now(timezone.utc) + HOLD_DURATION

            try:
                booking_row = await conn.fetchrow(
                    """
                    INSERT INTO bookings
                        (flight_id, seat_class_id, passenger_name, passenger_email,
                         num_seats, fare_type, price_total, currency, status,
                         idempotency_key, hold_expires_at)
                    VALUES ($1, $2, $3, $4, $5, $6::fare_type, $7, $8,
                            'held'::booking_status, $9, $10)
                    RETURNING *
                    """,
                    payload.flight_id,
                    payload.seat_class_id,
                    payload.passenger_name,
                    payload.passenger_email,
                    payload.num_seats,
                    payload.fare_type.value,
                    payload.price_total,
                    payload.currency,
                    payload.idempotency_key,
                    hold_expires_at,
                )
            except asyncpg.UniqueViolationError:
                # Lost a race against a concurrent identical retry between
                # our SELECT above and this INSERT; the other request's
                # commit wins and this one just needs to fetch it fresh.
                # (held_seats was already incremented by our own hold_seats()
                # call above; roll back this transaction to release it —
                # the FastAPI-owned release_expired_holds()/manual release
                # path is not invoked here since this is a same-key replay,
                # not an abandoned hold.)
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Duplicate idempotency_key insert race — retry the request",
                )

            return HoldResponse(
                booking_id=booking_row["id"],
                status=booking_row["status"],
                hold_expires_at=booking_row["hold_expires_at"],
                replayed=False,
            )


# ---------------------------------------------------------------------------
# POST /api/v1/bookings/confirm
# ---------------------------------------------------------------------------

@router.post("/bookings/confirm", response_model=ConfirmResponse)
async def confirm_booking_endpoint(
    payload: ConfirmRequest,
    pool: asyncpg.Pool = Depends(get_pool),
) -> ConfirmResponse:
    async with pool.acquire() as conn:
        async with conn.transaction():
            booking_row = await conn.fetchrow(
                "SELECT status FROM bookings WHERE id = $1", payload.booking_id
            )
            if booking_row is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")

            confirmed: bool = await conn.fetchval(
                "SELECT confirm_booking($1)", payload.booking_id
            )
            if not confirmed:
                # confirm_booking() returns FALSE only when the booking row
                # wasn't found in status='held' — i.e. it already expired,
                # was cancelled, or was already confirmed.
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Booking is not in a confirmable state "
                        f"(current status: {booking_row['status']})"
                    ),
                )

            return ConfirmResponse(booking_id=payload.booking_id, status="confirmed")


@router.get("/bookings/{booking_id}", response_model=BookingResponse)
async def get_booking(
    booking_id: UUID,
    pool: asyncpg.Pool = Depends(get_pool),
) -> BookingResponse:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM bookings WHERE id = $1", booking_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    return BookingResponse(**dict(row))


# ---------------------------------------------------------------------------
# POST /api/v1/waitlist
# ---------------------------------------------------------------------------

@router.post(
    "/waitlist",
    response_model=WaitlistResponse,
    status_code=status.HTTP_201_CREATED,
)
async def join_waitlist(
    payload: WaitlistRequest,
    pool: asyncpg.Pool = Depends(get_pool),
) -> WaitlistResponse:
    """FastAPI is the sole writer of waitlist INSERTs (n8n only UPDATEs
    status via promote_next_waitlisted(), on its own schedule — this
    endpoint never calls that function). We require the class to actually
    be full before accepting a join, so the waitlist can't silently become
    a way to reserve a seat that was in fact still available."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            seat_class = await conn.fetchrow(
                """
                SELECT (allocated_seats + overbook_buffer - booked_seats - held_seats)
                    AS available_seats
                FROM seat_classes
                WHERE id = $1 AND flight_id = $2
                FOR UPDATE
                """,
                payload.seat_class_id,
                payload.flight_id,
            )
            if seat_class is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="seat_class_id does not belong to flight_id",
                )
            if seat_class["available_seats"] > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Seats are still available in this class — book directly instead of waitlisting",
                )

            row = await conn.fetchrow(
                """
                INSERT INTO waitlist
                    (flight_id, seat_class_id, passenger_name, passenger_email, loyalty_tier)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING *
                """,
                payload.flight_id,
                payload.seat_class_id,
                payload.passenger_name,
                payload.passenger_email,
                payload.loyalty_tier,
            )
            return WaitlistResponse(**dict(row))