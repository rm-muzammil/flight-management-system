"""
Python mirrors of the Postgres ENUM types defined in 0001_init.sql.

Do NOT add members here without a matching migration — asyncpg will
happily send an unrecognized string to Postgres and let the database
reject it with a much less useful error than a Pydantic validation
error would have given the caller.
"""

from enum import Enum


class SeatClassName(str, Enum):
    first = "first"
    business = "business"
    economy = "economy"


class FlightStatus(str, Enum):
    scheduled = "scheduled"
    delayed = "delayed"
    cancelled = "cancelled"
    departed = "departed"


class BookingStatus(str, Enum):
    held = "held"
    confirmed = "confirmed"
    cancelled = "cancelled"
    refunded = "refunded"
    waitlisted = "waitlisted"


class FareType(str, Enum):
    basic_economy = "basic_economy"
    flexible = "flexible"
    business_flex = "business_flex"
    first_flex = "first_flex"


class WaitlistStatus(str, Enum):
    waiting = "waiting"
    notified = "notified"
    promoted = "promoted"
    expired = "expired"
    cancelled = "cancelled"


class AdminRole(str, Enum):
    super_admin = "super_admin"
    ops_agent = "ops_agent"