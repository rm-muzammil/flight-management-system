"""
Pydantic v2 schemas for app/routes/admin.py.

These map 1:1 onto `flights`, `seat_classes`, and `admin_audit_log` from
0001_init.sql. No column names invented here that aren't in that schema.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import FlightStatus, SeatClassName


class SeatAllocationInput(BaseModel):
    """One row of the seat_classes table, as supplied by an admin."""

    class_name: SeatClassName
    allocated_seats: int = Field(
        gt=0,
        description="Must be a positive integer — zero/negative allocations are rejected.",
    )
    overbook_buffer: int = Field(default=0, ge=0)


class FlightCreateRequest(BaseModel):
    flight_number: str = Field(min_length=1, max_length=10)
    origin: str = Field(min_length=3, max_length=8, description="IATA/ICAO code")
    destination: str = Field(min_length=3, max_length=8)
    departure_at: datetime
    arrival_at: datetime
    total_seats: int = Field(gt=0)
    seat_classes: list[SeatAllocationInput] = Field(min_length=1)
    created_by: UUID = Field(description="admins.id of the acting admin")

    @field_validator("seat_classes")
    @classmethod
    def _unique_class_names(cls, v: list[SeatAllocationInput]) -> list[SeatAllocationInput]:
        names = [sc.class_name for sc in v]
        if len(names) != len(set(names)):
            raise ValueError("duplicate seat class_name in seat_classes")
        return v

    @model_validator(mode="after")
    def _check_route_and_times(self) -> "FlightCreateRequest":
        if self.arrival_at <= self.departure_at:
            raise ValueError("arrival_at must be after departure_at")
        if self.origin.strip().upper() == self.destination.strip().upper():
            raise ValueError("origin and destination must differ")
        allocated_sum = sum(sc.allocated_seats for sc in self.seat_classes)
        if allocated_sum != self.total_seats:
            raise ValueError(
                f"seat_classes allocations sum to {allocated_sum}, "
                f"which does not match total_seats ({self.total_seats})"
            )
        return self


class SeatAllocationPatch(BaseModel):
    """Partial re-allocation used by PATCH — same row shape, all optional
    except the key that identifies which class is being changed."""

    class_name: SeatClassName
    allocated_seats: int | None = Field(default=None, gt=0)
    overbook_buffer: int | None = Field(default=None, ge=0)


class FlightUpdateRequest(BaseModel):
    """All fields optional — only the ones supplied are changed.
    seat_classes here is a partial patch list, not a full replacement."""

    origin: str | None = Field(default=None, min_length=3, max_length=8)
    destination: str | None = Field(default=None, min_length=3, max_length=8)
    departure_at: datetime | None = None
    arrival_at: datetime | None = None
    status: FlightStatus | None = None
    total_seats: int | None = Field(default=None, gt=0)
    seat_classes: list[SeatAllocationPatch] | None = None
    updated_by: UUID = Field(description="admins.id of the acting admin, for the audit log")

    @model_validator(mode="after")
    def _check_times_if_both_present(self) -> "FlightUpdateRequest":
        if self.departure_at and self.arrival_at and self.arrival_at <= self.departure_at:
            raise ValueError("arrival_at must be after departure_at")
        return self


class FlightCancelRequest(BaseModel):
    cancelled_by: UUID = Field(description="admins.id of the acting admin, for the audit log")
    reason: str | None = Field(default=None, max_length=500)


class SeatClassResponse(BaseModel):
    id: UUID
    class_name: SeatClassName
    allocated_seats: int
    booked_seats: int
    held_seats: int
    overbook_buffer: int

    model_config = {"from_attributes": True}


class FlightResponse(BaseModel):
    id: UUID
    flight_number: str
    origin: str
    destination: str
    departure_at: datetime
    arrival_at: datetime
    total_seats: int
    status: FlightStatus
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime
    seat_classes: list[SeatClassResponse] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class AuditLogEntry(BaseModel):
    id: UUID
    admin_id: UUID | None
    flight_id: UUID | None
    action: str
    old_value: dict[str, Any] | None
    new_value: dict[str, Any] | None
    created_at: datetime

    model_config = {"from_attributes": True}