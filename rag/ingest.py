"""
rag/ingest.py

Parses policy/fare-rule documents (markdown or plain text), chunks them,
generates embeddings, and upserts them into a Pinecone index.

Run:
    python -m rag.ingest --source ./policy_docs --index flight-policies

Environment variables required (see .env.example / README section below):
    PINECONE_API_KEY
    PINECONE_ENVIRONMENT      (e.g. "us-east-1", only needed for pod-based indexes)
    GEMINI_API_KEY            (used here only for embeddings, if using Gemini embeddings)
    EMBEDDING_PROVIDER        ("gemini" or "openai" — defaults to "gemini")
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from pinecone import Pinecone, ServerlessSpec

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("rag.ingest")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

PINECONE_API_KEY = os.environ.get("PINECONE_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
EMBEDDING_PROVIDER = os.environ.get("EMBEDDING_PROVIDER", "gemini")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "768"))  # gemini-embedding-001 supports 128-3072, truncated via output_dimensionality

CHUNK_SIZE_TOKENS = 350          # approx tokens per chunk
CHUNK_OVERLAP_TOKENS = 50        # overlap between consecutive chunks
BATCH_UPSERT_SIZE = 100

if not PINECONE_API_KEY:
    log.error("PINECONE_API_KEY is not set. Aborting.")
    sys.exit(1)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class Chunk:
    id: str
    text: str
    source_file: str
    chunk_index: int
    metadata: dict


# --------------------------------------------------------------------------- #
# Document loading & chunking
# --------------------------------------------------------------------------- #

def load_documents(source_dir: Path) -> Iterable[tuple[str, str]]:
    """Yield (filename, raw_text) for every .md/.txt file under source_dir."""
    for path in sorted(source_dir.rglob("*")):
        if path.suffix.lower() in {".md", ".txt"} and path.is_file():
            yield path.name, path.read_text(encoding="utf-8")


def _approx_token_count(text: str) -> int:
    # Cheap heuristic (~4 chars/token) — avoids pulling in a tokenizer dependency.
    return max(1, len(text) // 4)


def chunk_text(text: str, filename: str) -> list[Chunk]:
    """
    Splits on markdown headers first (so a fare-rule section stays together),
    then falls back to a sliding window over paragraphs for long sections.
    """
    # Split on markdown headers (#, ##, ###) while keeping the header with its body.
    sections = re.split(r"\n(?=#{1,3}\s)", text.strip())
    chunks: list[Chunk] = []
    chunk_idx = 0

    for section in sections:
        section = section.strip()
        if not section:
            continue

        if _approx_token_count(section) <= CHUNK_SIZE_TOKENS:
            chunks.append(_make_chunk(section, filename, chunk_idx))
            chunk_idx += 1
            continue

        # Sliding window over paragraphs for oversized sections.
        paragraphs = [p for p in section.split("\n\n") if p.strip()]
        buffer: list[str] = []
        buffer_tokens = 0

        for para in paragraphs:
            para_tokens = _approx_token_count(para)
            if buffer_tokens + para_tokens > CHUNK_SIZE_TOKENS and buffer:
                chunks.append(_make_chunk("\n\n".join(buffer), filename, chunk_idx))
                chunk_idx += 1
                # keep overlap: retain last paragraph(s) worth ~CHUNK_OVERLAP_TOKENS
                overlap_buf: list[str] = []
                overlap_tokens = 0
                for p in reversed(buffer):
                    overlap_tokens += _approx_token_count(p)
                    overlap_buf.insert(0, p)
                    if overlap_tokens >= CHUNK_OVERLAP_TOKENS:
                        break
                buffer = overlap_buf
                buffer_tokens = overlap_tokens

            buffer.append(para)
            buffer_tokens += para_tokens

        if buffer:
            chunks.append(_make_chunk("\n\n".join(buffer), filename, chunk_idx))
            chunk_idx += 1

    return chunks


def _make_chunk(text: str, filename: str, idx: int) -> Chunk:
    chunk_id = hashlib.sha256(f"{filename}:{idx}:{text[:50]}".encode()).hexdigest()[:24]
    header_match = re.match(r"^#{1,3}\s*(.+)", text)
    section_title = header_match.group(1).strip() if header_match else None
    return Chunk(
        id=chunk_id,
        text=text,
        source_file=filename,
        chunk_index=idx,
        metadata={
            "source_file": filename,
            "chunk_index": idx,
            "section_title": section_title or "",
            "text": text,  # stored so we can return it directly from a query, avoiding a second fetch
        },
    )


# --------------------------------------------------------------------------- #
# Embeddings
# --------------------------------------------------------------------------- #

def embed_texts(texts: list[str]) -> list[list[float]]:
    """
    Generates embeddings for a batch of texts.
    Swap the body of this function if you standardize on a different embedding model —
    the rest of the pipeline only depends on the returned list[list[float]] shape.
    """
    if EMBEDDING_PROVIDER == "gemini":
        return _embed_with_gemini(texts)
    raise ValueError(f"Unsupported EMBEDDING_PROVIDER: {EMBEDDING_PROVIDER}")


def _embed_with_gemini(texts: list[str]) -> list[list[float]]:
    from google import genai
    from google.genai import types

    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set but EMBEDDING_PROVIDER=gemini")

    client = genai.Client(api_key=GEMINI_API_KEY)
    embeddings: list[list[float]] = []

    # embed_content is called per-text; batch here to keep call count sane.
    for text in texts:
        result = client.models.embed_content(
            model="gemini-embedding-001",
            contents=text,
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_DOCUMENT",
                output_dimensionality=EMBEDDING_DIM,
            ),
        )
        embeddings.append(result.embeddings[0].values)
        time.sleep(0.05)  # light rate-limit courtesy

    return embeddings


# --------------------------------------------------------------------------- #
# Pinecone upsert
# --------------------------------------------------------------------------- #

def get_or_create_index(pc: Pinecone, index_name: str):
    existing = [idx["name"] for idx in pc.list_indexes()]
    if index_name not in existing:
        log.info("Creating Pinecone index '%s' (dim=%d)", index_name, EMBEDDING_DIM)
        pc.create_index(
            name=index_name,
            dimension=EMBEDDING_DIM,
            metric="cosine",
            spec=ServerlessSpec(
                cloud=os.environ.get("PINECONE_CLOUD", "aws"),
                region=os.environ.get("PINECONE_REGION", "us-east-1"),
            ),
        )
        # Wait for the index to be ready.
        while not pc.describe_index(index_name).status["ready"]:
            time.sleep(1)
    return pc.Index(index_name)


def upsert_chunks(index, chunks: list[Chunk]) -> None:
    for batch_start in range(0, len(chunks), BATCH_UPSERT_SIZE):
        batch = chunks[batch_start: batch_start + BATCH_UPSERT_SIZE]
        vectors = embed_texts([c.text for c in batch])
        payload = [
            {"id": c.id, "values": vec, "metadata": c.metadata}
            for c, vec in zip(batch, vectors)
        ]
        index.upsert(vectors=payload)
        log.info("Upserted %d/%d chunks", batch_start + len(batch), len(chunks))


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest policy docs into Pinecone.")
    parser.add_argument("--source", required=True, help="Directory of .md/.txt policy files")
    parser.add_argument("--index", required=True, help="Pinecone index name")
    args = parser.parse_args()

    source_dir = Path(args.source)
    if not source_dir.exists():
        log.error("Source directory does not exist: %s", source_dir)
        sys.exit(1)

    pc = Pinecone(api_key=PINECONE_API_KEY)
    index = get_or_create_index(pc, args.index)

    all_chunks: list[Chunk] = []
    for filename, text in load_documents(source_dir):
        file_chunks = chunk_text(text, filename)
        log.info("Chunked %s into %d chunk(s)", filename, len(file_chunks))
        all_chunks.extend(file_chunks)

    if not all_chunks:
        log.warning("No documents found in %s — nothing to ingest.", source_dir)
        return

    upsert_chunks(index, all_chunks)
    log.info("Ingestion complete: %d chunks upserted into '%s'.", len(all_chunks), args.index)


if __name__ == "__main__":
    main()