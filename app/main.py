"""
FastAPI entrypoint for the dual-writer Flight Management System.

This process is the *only* HTTP write path into Supabase Postgres for
live/transactional data (flights, seat_classes, bookings, admin_audit_log,
and waitlist INSERTs). n8n writes to the same database directly and
independently on its own schedule — there is no HTTP contract between
the two systems, only the shared schema and its row-locking functions.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.database import close_pool, connect_pool, get_pool
from app.routes import admin, booking
from app.routes.rag import router as rag_router



logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_pool()
    logger.info("Database pool ready")
    try:
        yield
    finally:
        await close_pool()
        logger.info("Database pool closed")


app = FastAPI(
    title="Flight Management System — FastAPI Core API",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS: tighten allow_origins to the real frontend origin(s) before deploying;
# "*" here is a local-dev default, not a production setting.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(admin.router)
app.include_router(booking.router)
app.include_router(rag_router, prefix="/api/v1")



@app.get("/health", tags=["infra"])
async def health_check() -> JSONResponse:
    """Liveness *and* readiness in one: also confirms the pool can reach
    Postgres, so a load balancer won't route traffic to an instance that's
    up but DB-disconnected."""
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception as exc:  # noqa: BLE001 — health check must not raise
        logger.warning("Health check DB probe failed: %s", exc)
        return JSONResponse(status_code=503, content={"status": "degraded", "database": "unreachable"})

    return JSONResponse(status_code=200, content={"status": "ok", "database": "connected"})