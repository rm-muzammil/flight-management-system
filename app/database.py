"""
Async connection pool for Supabase Postgres.

We use asyncpg directly rather than an ORM: every seat mutation in this
service goes through the hand-written SQL functions in 0001_init.sql
(hold_seats, confirm_booking, release_expired_holds, promote_next_waitlisted),
so there is no object-relational mapping layer to justify — just
parameterized SQL and asyncpg's native connection pooling.

Note on enums: asyncpg does not automatically know how to encode a
Python str/Enum into a Postgres ENUM type; every query below that
writes an enum column casts the parameter explicitly (e.g. `$3::flight_status`)
rather than relying on implicit coercion.

Config source: reads a .env file (via python-dotenv) if one is present,
then DATABASE_URL — set this to the connection string from your Supabase
project's Settings -> Database -> Connection string (URI format). Falls
back to building one from split POSTGRES_USER/PASSWORD/HOST/PORT/DB vars
(the shape docker-compose.yml in Chat 5 will also use) if DATABASE_URL
isn't set — that fallback is for local/Dockerized Postgres, not Supabase.

Supabase-specific handling, both driven off DATABASE_URL alone (no
extra env vars needed):

1. SSL — Supabase requires SSL on every external connection. If the host
   looks like a Supabase host and the DSN doesn't already specify
   `sslmode`, we append `sslmode=require`.
2. Pooler compatibility — Supabase's connection pooler (Supavisor/pgbouncer,
   normally port 6543, "transaction" mode) does not support prepared
   statements, but asyncpg prepares every parameterized query by default.
   If the DSN's port is 6543 (or the host contains "pooler"), we pass
   statement_cache_size=0 to disable that, matching Supabase's own asyncpg
   guidance. Connecting on port 5432 (direct connection, or the "Session"
   pooler mode) does not need this and gets normal statement caching.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg
from dotenv import load_dotenv

load_dotenv()  # no-op if there's no .env file; never overrides already-set env vars

logger = logging.getLogger("app.database")


def _build_database_url() -> str:
    if url := os.environ.get("DATABASE_URL"):
        return url

    try:
        user = os.environ["POSTGRES_USER"]
        password = os.environ["POSTGRES_PASSWORD"]
        db = os.environ["POSTGRES_DB"]
    except KeyError as exc:
        raise RuntimeError(
            "Set either DATABASE_URL (e.g. your Supabase connection string), or "
            "POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB (+ optional POSTGRES_HOST/"
            "POSTGRES_PORT) in your environment or .env file."
        ) from exc

    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    # Percent-encode here too — POSTGRES_PASSWORD can contain URI-reserved
    # characters (@, #, %, :, /) just as easily as a pasted DATABASE_URL can.
    return (
        f"postgresql://{urllib.parse.quote(user, safe='')}:"
        f"{urllib.parse.quote(password, safe='')}@{host}:{port}/{db}"
    )


def _is_supabase_host(host: str) -> bool:
    return "supabase.co" in host or "supabase.com" in host


def _uses_transaction_pooler(host: str, port: int | None) -> bool:
    # Port, not hostname, is what actually distinguishes the two Supavisor
    # modes: the session pooler (port 5432) and transaction pooler (port
    # 6543) share the same "...pooler.supabase.com" host pattern, but only
    # transaction mode lacks prepared-statement support. Keying off "pooler"
    # in the hostname alone would wrongly disable statement caching for
    # session-pooler connections too.
    return port == 6543


def _split_credentials_and_hostpart(dsn: str) -> tuple[str, str, str]:
    """Manually splits a postgres:// DSN into (scheme, userinfo, hostpart),
    WITHOUT assuming the password is already percent-encoded.

    We can't just hand the raw DSN to urlsplit()/asyncpg and trust it:
    Supabase-generated passwords routinely contain URI-reserved characters
    (@, #, %, :, /) verbatim, and both Python's URL parser and asyncpg's own
    DSN parser assume those are delimiters unless percent-encoded — an
    unescaped '@' or '#' in the password silently shifts where "host" and
    "port" get parsed from, which is exactly the bug this function exists
    to avoid. Splitting on the LAST '@' before the host section is safe
    because a real hostname never contains '@', even if the password does.
    """
    scheme, _, rest = dsn.partition("://")
    userinfo, sep, hostpart = rest.rpartition("@")
    if not sep:
        raise ValueError("DATABASE_URL is missing the '@' separating credentials from host")
    return scheme, userinfo, hostpart


def _prepare_dsn_and_connect_kwargs(raw_dsn: str) -> tuple[str, dict]:
    """Re-encodes the DSN's user/password (in case they contain unescaped
    URI-reserved characters), adds sslmode=require for Supabase hosts (if
    not already set), and returns extra asyncpg connect kwargs
    (statement_cache_size=0 when the DSN points at Supabase's
    transaction-mode pooler)."""
    scheme, userinfo, hostpart = _split_credentials_and_hostpart(raw_dsn)

    user, sep, password = userinfo.partition(":")
    if sep:
        # Re-quote unconditionally: quoting an already-quoted "%40" leaves
        # it as "%2540" (double-encoded), which is wrong — so unquote first,
        # then quote, which is a no-op for already-correct input and fixes
        # raw unescaped input either way.
        user_q = urllib.parse.quote(urllib.parse.unquote(user), safe="")
        password_q = urllib.parse.quote(urllib.parse.unquote(password), safe="")
        userinfo = f"{user_q}:{password_q}"

    # hostpart is now safe to hand to urlsplit — the credentials that could
    # contain reserved characters have already been extracted above.
    parsed = urllib.parse.urlsplit(f"//{hostpart}")
    host = parsed.hostname or ""
    query = urllib.parse.parse_qs(parsed.query)

    if _is_supabase_host(host) and "sslmode" not in query:
        query["sslmode"] = ["require"]
        new_query = urllib.parse.urlencode(query, doseq=True)
        parsed = parsed._replace(query=new_query)
        hostpart = urllib.parse.urlunsplit(parsed).lstrip("/")

    raw_dsn = f"{scheme}://{userinfo}@{hostpart}"

    connect_kwargs: dict = {}

    if _is_supabase_host(host) and _uses_transaction_pooler(host, parsed.port):
        connect_kwargs["statement_cache_size"] = 0
        logger.info(
            "Detected Supabase transaction pooler (port %s) — disabling asyncpg "
            "statement cache, since pgbouncer transaction mode doesn't support "
            "prepared statements.",
            parsed.port,
        )

    return raw_dsn, connect_kwargs


DATABASE_URL, _EXTRA_CONNECT_KWARGS = _prepare_dsn_and_connect_kwargs(_build_database_url())
DB_POOL_MIN_SIZE = int(os.environ.get("DB_POOL_MIN_SIZE", "2"))
DB_POOL_MAX_SIZE = int(os.environ.get("DB_POOL_MAX_SIZE", "10"))
DB_COMMAND_TIMEOUT = float(os.environ.get("DB_COMMAND_TIMEOUT", "10"))

_pool: asyncpg.Pool | None = None


async def connect_pool() -> asyncpg.Pool:
    """Create the module-level pool. Call once, from the FastAPI lifespan."""
    global _pool
    if _pool is not None:
        return _pool

    logger.info("Creating asyncpg pool (min=%s, max=%s)", DB_POOL_MIN_SIZE, DB_POOL_MAX_SIZE)
    _pool = await asyncpg.create_pool(
        dsn=DATABASE_URL,
        min_size=DB_POOL_MIN_SIZE,
        max_size=DB_POOL_MAX_SIZE,
        command_timeout=DB_COMMAND_TIMEOUT,
        **_EXTRA_CONNECT_KWARGS,
    )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        logger.info("Closing asyncpg pool")
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    """FastAPI dependency — raises if called before the lifespan has started the pool."""
    if _pool is None:
        raise RuntimeError("Database pool has not been initialized yet")
    return _pool


@asynccontextmanager
async def acquire_connection() -> AsyncIterator[asyncpg.Connection]:
    """Convenience context manager for routes that need a single connection
    (e.g. to open an explicit transaction across several statements)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        yield conn