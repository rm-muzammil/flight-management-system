#!/usr/bin/env bash
# Seeds demo data for the Flight Management System presentation.
# Creates flights through POST /admin/flights (not raw SQL) so every
# flight always has matching seat_classes — no repeat of the
# PK-786/787/788/789 empty-seat_availability problem.
#
# Usage:
#   export ADMIN_ID="e2af730c-6801-40b9-92bc-55bbca4852c9"
#   export API_BASE="http://localhost:8000"   # or your Railway URL
#   ./seed_demo_data.sh

set -euo pipefail

: "${ADMIN_ID:?Set ADMIN_ID to a real row in the admins table first}"
API_BASE="${API_BASE:-http://localhost:8000}"

create_flight() {
  local body="$1"
  echo "Creating flight..."
  curl -s -X POST "$API_BASE/api/v1/admin/flights" \
    -H "Content-Type: application/json" \
    -H "X-Admin-Id: $ADMIN_ID" \
    -d "$body" | python3 -m json.tool
  echo "---"
}

# 1. A healthy, mostly-available flight — good for a plain search demo.
create_flight '{
  "flight_number": "PK-701",
  "origin": "LHE",
  "destination": "KHI",
  "departure_at": "2026-10-15T06:00:00Z",
  "arrival_at": "2026-10-15T08:00:00Z",
  "total_seats": 120,
  "seat_classes": [
    {"class_name": "first", "allocated_seats": 8},
    {"class_name": "business", "allocated_seats": 22},
    {"class_name": "economy", "allocated_seats": 90}
  ],
  "created_by": "'"$ADMIN_ID"'"
}'

# 2. An international route with all three classes — good for showing
#    the fuller FlightResponse/FlightSearchResult shape in a demo.
create_flight '{
  "flight_number": "PK-880",
  "origin": "ISB",
  "destination": "DXB",
  "departure_at": "2026-10-20T09:30:00Z",
  "arrival_at": "2026-10-20T12:45:00Z",
  "total_seats": 180,
  "seat_classes": [
    {"class_name": "first", "allocated_seats": 12},
    {"class_name": "business", "allocated_seats": 38},
    {"class_name": "economy", "allocated_seats": 130}
  ],
  "created_by": "'"$ADMIN_ID"'"
}'

# 3. Deliberately tiny economy allocation (3 seats) — hold all 3 live in
#    the demo, then show the waitlist-gate 409 flipping to a successful
#    join once the class is genuinely full. Good "we thought about edge
#    cases" moment for judges.
create_flight '{
  "flight_number": "PK-455",
  "origin": "KHI",
  "destination": "LHE",
  "departure_at": "2026-10-18T14:00:00Z",
  "arrival_at": "2026-10-18T16:00:00Z",
  "total_seats": 33,
  "seat_classes": [
    {"class_name": "first", "allocated_seats": 10},
    {"class_name": "business", "allocated_seats": 20},
    {"class_name": "economy", "allocated_seats": 3}
  ],
  "created_by": "'"$ADMIN_ID"'"
}'

# 4. A flight with an explicit overbook_buffer — good for demonstrating
#    that available_seats = allocated_seats + overbook_buffer - booked - held.
create_flight '{
  "flight_number": "PK-612",
  "origin": "LHE",
  "destination": "ISB",
  "departure_at": "2026-10-22T18:00:00Z",
  "arrival_at": "2026-10-22T19:15:00Z",
  "total_seats": 100,
  "seat_classes": [
    {"class_name": "business", "allocated_seats": 25, "overbook_buffer": 2},
    {"class_name": "economy", "allocated_seats": 75, "overbook_buffer": 5}
  ],
  "created_by": "'"$ADMIN_ID"'"
}'

# 5. A near-future flight (a few days out) — makes GET /api/v1/flights'
#    ordering-by-departure_at visibly obvious against the further-out ones above.
create_flight '{
  "flight_number": "PK-320",
  "origin": "DXB",
  "destination": "LHE",
  "departure_at": "2026-09-12T11:00:00Z",
  "arrival_at": "2026-09-12T15:30:00Z",
  "total_seats": 90,
  "seat_classes": [
    {"class_name": "business", "allocated_seats": 20},
    {"class_name": "economy", "allocated_seats": 70}
  ],
  "created_by": "'"$ADMIN_ID"'"
}'

echo "Done. Verify with:"
echo "  curl \"$API_BASE/api/v1/flights?limit=20\""
