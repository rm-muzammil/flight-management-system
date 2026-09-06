import asyncio
import asyncpg
import os
from datetime import datetime, timedelta, timezone

async def seed():
    conn = await asyncpg.connect(os.environ['DATABASE_URL'])

    await conn.execute(
        "DELETE FROM flights WHERE id = $1",
        "17308cbc-36bc-4fc9-8316-c0661f88c96f",
    )
    print("Cleaned up orphaned flight from previous attempt")

    departure = datetime.now(timezone.utc) + timedelta(days=10)
    arrival = departure + timedelta(hours=3)
    total_seats = 150

    flight_id = await conn.fetchval(
        """
        INSERT INTO flights (flight_number, origin, destination, departure_at, arrival_at, total_seats, status)
        VALUES ($1, $2, $3, $4, $5, $6, 'scheduled')
        RETURNING id
        """,
        "TP-1023", "KHI", "DXB", departure, arrival, total_seats,
    )
    print("Created flight:", flight_id)

    seat_class_id = await conn.fetchval(
        """
        INSERT INTO seat_classes (flight_id, class_name, allocated_seats, booked_seats, held_seats, overbook_buffer)
        VALUES ($1, 'economy', $2, 1, 0, 5)
        RETURNING id
        """,
        flight_id, total_seats,
    )
    print("Created seat_class:", seat_class_id)

    booking_id = await conn.fetchval(
        """
        INSERT INTO bookings
            (flight_id, seat_class_id, passenger_name, passenger_email, num_seats,
             fare_type, price_total, currency, status, idempotency_key)
        VALUES ($1, $2, $3, $4, $5, 'basic_economy', $6, 'USD', 'confirmed', $7)
        RETURNING id
        """,
        flight_id, seat_class_id, "Test Passenger", "test@example.com", 1,
        250.00, "test-seed-001",
    )
    print("Created booking:", booking_id)
    print("\nUse this booking_id for testing:", booking_id)

    await conn.close()

asyncio.run(seed())
