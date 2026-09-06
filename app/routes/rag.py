"""
app/routes/rag.py

FastAPI router for the policy-support RAG endpoints. Wired into the main app with:

    from app.routes.rag import router as rag_router
    app.include_router(rag_router, prefix="/api/v1")

The router declares its own "/rag" prefix internally, so the final paths are:
    POST /api/v1/rag/policy-question
    POST /api/v1/rag/policy-question/{draft_id}/approve-and-send

Shares the app's existing asyncpg pool via app.database.get_pool() rather than
opening a separate connection pool — service.py's core functions accept a pool
as a parameter for exactly this reason, so they work identically whether
called from this router or from a standalone script (see rag/service.py's
_example() function, which still creates its own pool for standalone testing
outside the app).

Persists drafts to the real `policy_drafts` table (columns verified against
the live schema: id serial, booking_id uuid, question, draft_response,
requires_human_approval boolean, status varchar, approved_by varchar,
created_at). Note the table has no columns for `sources` or
`approval_reason` — those are returned in the API response but not
persisted; add columns to policy_drafts if you want them stored for audit.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pinecone import Pinecone
from pydantic import BaseModel, Field

from app.database import get_pool
from rag.service import (
    BookingNotFoundError,
    PINECONE_API_KEY,
    answer_policy_question,
    PolicyResponsePayload,
)

router = APIRouter(prefix="/rag", tags=["policy-rag"])

# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #

async def get_db_pool() -> asyncpg.Pool:
    """
    Shares the app's existing connection pool rather than opening a new one.
    If app.database.get_pool() is async in your actual implementation
    (e.g. it lazily creates the pool on first call), change this to:
        return await get_pool()
    """
    return get_pool()


@lru_cache
def _pinecone_singleton() -> Pinecone:
    # Cached so a new client isn't constructed on every request.
    return Pinecone(api_key=PINECONE_API_KEY)


def get_pinecone_client() -> Pinecone:
    return _pinecone_singleton()


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #

class PolicyQuestionRequest(BaseModel):
    booking_id: str = Field(..., description="bookings.id (uuid) for the passenger asking the question")
    question: str = Field(..., min_length=1, max_length=2000)


class PolicyQuestionResponse(BaseModel):
    draft_id: int
    booking_id: str
    question: str
    draft_response: str
    sources: list[dict]
    requires_human_approval: bool
    approval_reason: str


class ApproveAndSendRequest(BaseModel):
    approved_by: str = Field(..., description="Agent/user id or email approving the draft")
    edited_response: Optional[str] = Field(
        None, description="If the human agent edited the draft before sending, pass the final text here."
    )


class ApproveAndSendResponse(BaseModel):
    draft_id: int
    status: str
    sent_to: Optional[str] = None


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@router.post("/policy-question", response_model=PolicyQuestionResponse)
async def create_policy_draft(
    body: PolicyQuestionRequest,
    pool: asyncpg.Pool = Depends(get_db_pool),
    pc: Pinecone = Depends(get_pinecone_client),
) -> PolicyQuestionResponse:
    """
    Generates a draft customer response grounded in the passenger's actual
    booking + fare rules and relevant policy text, and persists it to
    policy_drafts with status='pending'. Does NOT send anything — the draft
    must go through /approve-and-send.
    """
    try:
        payload: PolicyResponsePayload = await answer_policy_question(
            pool=pool,
            pc=pc,
            booking_id=body.booking_id,
            question=body.question,
        )
    except BookingNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    async with pool.acquire() as conn:
        draft_id = await conn.fetchval(
            """
            INSERT INTO policy_drafts
                (booking_id, question, draft_response, requires_human_approval, status)
            VALUES ($1, $2, $3, $4, 'pending')
            RETURNING id
            """,
            body.booking_id,
            payload.question,
            payload.draft_response,
            payload.requires_human_approval,
        )

    return PolicyQuestionResponse(
        draft_id=draft_id,
        booking_id=payload.booking_id,
        question=payload.question,
        draft_response=payload.draft_response,
        sources=payload.sources,
        requires_human_approval=payload.requires_human_approval,
        approval_reason=payload.approval_reason,
    )


@router.post("/policy-question/{draft_id}/approve-and-send", response_model=ApproveAndSendResponse)
async def approve_and_send_draft(
    draft_id: int,
    body: ApproveAndSendRequest,
    pool: asyncpg.Pool = Depends(get_db_pool),
) -> ApproveAndSendResponse:
    """
    Human-approval gate. This is the ONLY route allowed to trigger a Gmail send.
    Wire the actual send into Chat 3's n8n Gmail workflow (recommended — keeps
    Gmail credentials out of the FastAPI app) or call the Gmail API directly here.
    """
    async with pool.acquire() as conn:
        draft = await conn.fetchrow(
            "SELECT id, booking_id, draft_response, status FROM policy_drafts WHERE id = $1",
            draft_id,
        )
        if draft is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found")
        if draft["status"] == "sent":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Draft already sent")

        final_text = body.edited_response or draft["draft_response"]

        # --- Integration point -------------------------------------------------
        # Option A: trigger Chat 3's n8n webhook that owns the Gmail node, passing
        #           final_text + booking_id, and let n8n handle the send + logging.
        # Option B: call the Gmail API directly here if this service owns Gmail creds.
        #
        # send_result = await trigger_n8n_gmail_webhook(booking_id=draft["booking_id"], body=final_text)
        # ------------------------------------------------------------------------

        await conn.execute(
            """
            UPDATE policy_drafts
            SET status = 'sent', approved_by = $1, draft_response = $2
            WHERE id = $3
            """,
            body.approved_by,
            final_text,
            draft_id,
        )

    return ApproveAndSendResponse(draft_id=draft_id, status="sent", sent_to=None)