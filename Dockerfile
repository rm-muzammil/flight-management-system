# =========================================================
# Dual-Writer Flight Management System — FastAPI Backend
# Multi-stage production Dockerfile
# =========================================================

# ---------- Stage 1: Builder ----------
FROM python:3.11-slim AS builder

WORKDIR /build

# System deps needed only for building wheels (kept out of final image)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Leverage Docker layer caching: copy only requirements first
COPY requirements.txt .

# Build wheels for all dependencies (includes fastapi, uvicorn, supabase,
# pinecone-client, google-generativeai, and whatever rag/ needs)
RUN pip install --no-cache-dir --upgrade pip \
    && pip wheel --no-cache-dir --wheel-dir /build/wheels -r requirements.txt


# ---------- Stage 2: Runtime ----------
FROM python:3.11-slim AS runtime

# Metadata
LABEL maintainer="Rm" \
      description="FastAPI backend for Dual-Writer Flight Management System"

# Create a non-root user to run the app
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

WORKDIR /app

# Minimal runtime system deps (curl kept for the HEALTHCHECK)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy pre-built wheels from the builder stage and install (no compiler needed here)
COPY --from=builder /build/wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

# Copy application source, including the rag/ module
COPY app/ ./app/
COPY rag/ ./rag/

# Ownership + drop to non-root
RUN chown -R appuser:appuser /app
USER appuser

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD sh -c 'curl -f http://localhost:${PORT:-8000}/health || exit 1'

# Use shell form so $PORT is expanded at runtime — Railway (and most PaaS)
# inject their own PORT value, which won't always be 8000.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 2"]
