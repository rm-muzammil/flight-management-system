"""
Pydantic v2 schemas for app/routes/booking.py.

Everything that mutates seat counts intentionally has NO field for
booked_seats/held_seats — those only ever move via hold_seats() /
confirm_booking() in Postgres, never via a value the client sends.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.models.enums import BookingStatus, FareType, SeatClassName, WaitlistStatus


# ---- search --------------------------------------------------------------

class SeatAvailability(BaseModel):
    seat_class_id: UUID
    class_name: SeatClassName
    available_seats: int = Field(
        description="allocated_seats + overbook_buffer - booked_seats - held_seats"
    )
    allocated_seats: int
    overbook_buffer: int


class FlightSearchResult(BaseModel):
    flight_id: UUID
    flight_number: str
    origin: str
    destination: str
    departure_at: datetime
    arrival_at: datetime
    status: str
    seat_availability: list[SeatAvailability]

    model_config = {"from_attributes": True}


class SearchQuery(BaseModel):
    origin: str = Field(min_length=3, max_length=8)
    destination: str = Field(min_length=3, max_length=8)
    departure_date: date


# ---- hold / confirm --------------------------------------------------------

class HoldRequest(BaseModel):
    flight_id: UUID
    seat_class_id: UUID
    num_seats: int = Field(gt=0)
    passenger_name: str = Field(min_length=1, max_length=200)
    passenger_email: EmailStr
    fare_type: FareType
    price_total: float = Field(ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    idempotency_key: str = Field(min_length=8, max_length=200)


class HoldResponse(BaseModel):
    booking_id: UUID
    status: BookingStatus
    hold_expires_at: datetime | None
    replayed: bool = Field(
        default=False,
        description="True if this response came from a prior request with the same idempotency_key.",
    )


class ConfirmRequest(BaseModel):
    booking_id: UUID


class ConfirmResponse(BaseModel):
    booking_id: UUID
    status: BookingStatus


class BookingResponse(BaseModel):
    id: UUID
    flight_id: UUID
    seat_class_id: UUID
    passenger_name: str
    passenger_email: str
    num_seats: int
    fare_type: FareType
    price_total: float
    currency: str
    status: BookingStatus
    idempotency_key: str
    hold_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---- waitlist --------------------------------------------------------------

class WaitlistRequest(BaseModel):
    flight_id: UUID
    seat_class_id: UUID
    passenger_name: str = Field(min_length=1, max_length=200)
    passenger_email: EmailStr
    loyalty_tier: int = Field(default=0, ge=0)


class WaitlistResponse(BaseModel):
    id: UUID
    flight_id: UUID
    seat_class_id: UUID
    passenger_name: str
    passenger_email: str
    loyalty_tier: int
    status: WaitlistStatus
    created_at: datetime

    model_config = {"from_attributes": True}