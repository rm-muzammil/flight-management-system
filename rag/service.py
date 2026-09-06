"""
rag/service.py

Policy-support RAG service.

Flow for answer_policy_question():
    1. Fetch the passenger's ACTUAL booking record from Postgres (fare class,
       fare rules, flight info) — this grounds the answer in the real booking
       rather than a generic policy match.
    2. Query Pinecone for the policy text chunks relevant to the question,
       optionally filtered/boosted by the booking's fare class.
    3. Combine booking facts + retrieved policy text into a prompt and call
       Gemini (gemini-3.6-flash) to draft a customer-facing response.
    4. Return a structured payload with the draft and a `requires_human_approval`
       flag — nothing is sent via Gmail automatically.

NOTE ON SCHEMA (verified against the live database):
    bookings: id (uuid), flight_id (uuid), seat_class_id (uuid), passenger_name,
              passenger_email, num_seats, fare_type (enum), price_total, currency,
              status (enum), idempotency_key, hold_expires_at, created_at, updated_at
    flights:  id (uuid), flight_number, origin, destination, departure_at, arrival_at,
              total_seats, status (enum), created_by, created_at, updated_at
    seat_classes: id (uuid), flight_id (uuid), class_name (enum), allocated_seats,
              booked_seats, held_seats, overbook_buffer, updated_at

    There is no fare_rules_json column anywhere in the schema — fare rules
    themselves live in the policy documents (Pinecone), not in Postgres.
    Postgres's job here is to tell us WHICH rule applies to this passenger
    (their seat_classes.class_name and bookings.fare_type), not what the rule says.

Environment variables required:
    DATABASE_URL        Postgres/Supabase connection string (asyncpg format)
    PINECONE_API_KEY
    GEMINI_API_KEY
    POLICY_INDEX_NAME   Pinecone index name created by rag/ingest.py
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from typing import Any, Optional

import asyncpg
from pinecone import Pinecone

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("rag.service")

DATABASE_URL = os.environ.get("DATABASE_URL")
PINECONE_API_KEY = os.environ.get("PINECONE_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
POLICY_INDEX_NAME = os.environ.get("POLICY_INDEX_NAME", "flight-policies")

TOP_K_POLICY_CHUNKS = 5

# booking_id, flight_id, seat_class_id are all uuid — asyncpg accepts a plain
# Python str containing a valid UUID for a uuid-typed parameter, so booking_id
# is typed as str throughout this file rather than int or uuid.UUID.
BOOKING_QUERY = """
    SELECT
        b.id                    AS booking_id,
        sc.class_name           AS fare_class,
        b.fare_type,
        b.price_total,
        b.currency,
        b.num_seats,
        b.status                AS booking_status,
        f.flight_number,
        f.origin,
        f.destination,
        f.departure_at          AS departure_time,
        f.arrival_at            AS arrival_time
    FROM bookings b
    JOIN flights f ON f.id = b.flight_id
    LEFT JOIN seat_classes sc ON sc.id = b.seat_class_id
    WHERE b.id = $1
"""


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class PolicyResponsePayload:
    booking_id: str
    question: str
    draft_response: str
    sources: list[dict]
    requires_human_approval: bool
    approval_reason: str

    def to_dict(self) -> dict:
        return asdict(self)


class BookingNotFoundError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Postgres: fetch the real booking / fare rule
# --------------------------------------------------------------------------- #

async def fetch_booking_context(pool: asyncpg.Pool, booking_id: str) -> dict[str, Any]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(BOOKING_QUERY, booking_id)

    if row is None:
        raise BookingNotFoundError(f"No booking found for booking_id={booking_id}")

    return dict(row)


# --------------------------------------------------------------------------- #
# Pinecone: retrieve relevant policy text
# --------------------------------------------------------------------------- #

def _embed_query(question: str) -> list[float]:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)
    result = client.models.embed_content(
        model="gemini-embedding-001",
        contents=question,
        config=types.EmbedContentConfig(
            task_type="RETRIEVAL_QUERY",
            output_dimensionality=int(os.environ.get("EMBEDDING_DIM", "768")),
        ),
    )
    return result.embeddings[0].values


def fetch_policy_context(pc: Pinecone, question: str, fare_class: Optional[str]) -> list[dict]:
    index = pc.Index(POLICY_INDEX_NAME)
    query_vector = _embed_query(question)

    # NOTE: fare_class filtering is intentionally NOT applied here. ingest.py
    # does not currently tag chunks with a fare_class metadata field (it only
    # stores source_file, chunk_index, section_title, text), so filtering on
    # it would exclude every vector and always return zero matches — which is
    # exactly what happened before this was removed. To re-enable fare-class-
    # aware retrieval: (1) add a `fare_class` field to each chunk's metadata
    # in ingest.py (e.g. from a front-matter convention in the policy .md
    # files, or a filename convention), then (2) restore a filter here like
    # `query_kwargs["filter"] = {"fare_class": {"$in": [fare_class, "ALL"]}}`.
    query_kwargs: dict[str, Any] = {
        "vector": query_vector,
        "top_k": TOP_K_POLICY_CHUNKS,
        "include_metadata": True,
    }

    result = index.query(**query_kwargs)
    matches = result.get("matches", []) if isinstance(result, dict) else result.matches

    policy_chunks = []
    for m in matches:
        metadata = m["metadata"] if isinstance(m, dict) else m.metadata
        score = m["score"] if isinstance(m, dict) else m.score
        policy_chunks.append(
            {
                "text": metadata.get("text", ""),
                "source_file": metadata.get("source_file", ""),
                "section_title": metadata.get("section_title", ""),
                "score": score,
            }
        )
    return policy_chunks


# --------------------------------------------------------------------------- #
# Gemini: draft the customer response
# --------------------------------------------------------------------------- #

def _build_prompt(question: str, booking: dict[str, Any], policy_chunks: list[dict]) -> str:
    policy_text = "\n\n---\n\n".join(
        f"[Source: {c['source_file']} — {c['section_title']}]\n{c['text']}" for c in policy_chunks
    )

    return f"""You are a customer support assistant for an airline. Draft a clear, polite,
accurate response to the customer's question below.

This passenger's booking has a specific seat class and fare type — these determine
WHICH policy applies to them. Use the general policy excerpts below to determine WHAT
that policy actually says for their seat class / fare type combination. Do not apply
a policy excerpt to this booking if it clearly applies to a different seat class or
fare type than the one shown below.

Do not invent any fees, deadlines, or exceptions not present in the provided policy
excerpts. If the policy excerpts don't clearly cover this booking's seat class or fare
type, say so and note that a human agent will follow up with the exact terms.

CUSTOMER QUESTION:
{question}

BOOKING DETAILS (ground truth for which rule applies):
- Booking ID: {booking['booking_id']}
- Flight: {booking.get('flight_number')} ({booking.get('origin')} -> {booking.get('destination')})
- Departure: {booking.get('departure_time')}
- Seat class: {booking.get('fare_class')}
- Fare type: {booking.get('fare_type')}
- Number of seats: {booking.get('num_seats')}
- Price paid: {booking.get('price_total')} {booking.get('currency')}
- Booking status: {booking.get('booking_status')}

RELEVANT GENERAL POLICY EXCERPTS (ground truth for what the rule says):
{policy_text if policy_text else "(none retrieved)"}

Write only the customer-facing response text, no preamble.
"""


def draft_response_with_gemini(question: str, booking: dict[str, Any], policy_chunks: list[dict]) -> str:
    from google import genai

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = _build_prompt(question, booking, policy_chunks)
    response = client.models.generate_content(model="gemini-3.6-flash", contents=prompt)
    return response.text.strip()


# --------------------------------------------------------------------------- #
# Approval-flag logic
# --------------------------------------------------------------------------- #

def _needs_human_approval(booking: dict[str, Any], policy_chunks: list[dict], draft: str) -> tuple[bool, str]:
    """
    Conservative by design: every draft requires human approval before sending.
    Kept as an explicit function (rather than a hardcoded True) so specific
    auto-approval rules can be added later without touching the rest of the pipeline.
    """
    if not policy_chunks:
        return True, "No policy sources were retrieved to support the draft."
    if booking.get("booking_status") not in ("confirmed", "ticketed"):
        return True, f"Booking status is '{booking.get('booking_status')}' — requires review."
    return True, "All customer-facing responses require human approval before sending (GDPR/quality control)."


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

async def answer_policy_question(
    pool: asyncpg.Pool,
    pc: Pinecone,
    booking_id: str,
    question: str,
) -> PolicyResponsePayload:
    booking = await fetch_booking_context(pool, booking_id)
    policy_chunks = fetch_policy_context(pc, question, booking.get("fare_class"))
    draft = draft_response_with_gemini(question, booking, policy_chunks)
    requires_approval, reason = _needs_human_approval(booking, policy_chunks, draft)

    return PolicyResponsePayload(
        booking_id=booking_id,
        question=question,
        draft_response=draft,
        sources=[
            {"source_file": c["source_file"], "section_title": c["section_title"], "score": c["score"]}
            for c in policy_chunks
        ],
        requires_human_approval=requires_approval,
        approval_reason=reason,
    )


# --------------------------------------------------------------------------- #
# Example usage (e.g. called from a FastAPI route in Chat 2's app)
# --------------------------------------------------------------------------- #

async def _example():
    import json

    pool = await asyncpg.create_pool(DATABASE_URL)
    pc = Pinecone(api_key=PINECONE_API_KEY)

    # Grabs the first booking in the table so this runs without hardcoding a
    # real booking_id. Replace with a specific booking_id once you're testing
    # a particular scenario.
    async with pool.acquire() as conn:
        sample = await conn.fetchrow("SELECT id FROM bookings LIMIT 1")
    if sample is None:
        raise RuntimeError("No rows in bookings — insert a test booking first.")

    payload = await answer_policy_question(
        pool=pool,
        pc=pc,
        booking_id=str(sample["id"]),
        question="Can I get a refund if I cancel 3 days before departure?",
    )
    print(json.dumps(payload.to_dict(), indent=2, default=str))
    await pool.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(_example())