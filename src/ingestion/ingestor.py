# src/ingestion/ingestor.py
"""
Stores EmbeddedChunk objects into PostgreSQL.

Tables written to:
  documents — one row per source file, tracks what's been ingested
  chunks    — one row per chunk, stores content + embedding + metadata

Design principles:
  Idempotent — safe to run multiple times, won't create duplicates
  Transactional — all chunks for a document commit together or not at all
  Observable — prints progress so you know what's happening
"""

from __future__ import annotations
import os
import psycopg2
from psycopg2.extras import execute_values
from pgvector.psycopg2 import register_vector
from dotenv import load_dotenv
from pathlib import Path

from src.ingestion.chunker import chunk_document
from src.ingestion.embedder import Embedder, EmbeddedChunk

load_dotenv()


# ── Database connection ───────────────────────────────────────────────────────

def get_connection():
    """
    Opens a PostgreSQL connection using DATABASE_URL from .env

    psycopg2 is Python's standard PostgreSQL driver.
    It translates Python types → SQL types and back.

    After connecting, register_vector() teaches psycopg2 how to
    serialize Python lists as pgvector's vector type.
    Without this, inserting embeddings would raise a type error.
    """
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    register_vector(conn)   # one-time call per connection — enables vector type
    return conn


# ── Document insertion ────────────────────────────────────────────────────────

def insert_document(cursor, filename: str, source_type: str) -> int:
    """
    Inserts a document record and returns its auto-generated ID.

    ON CONFLICT DO NOTHING — if the filename already exists, skip silently.
    RETURNING id — PostgreSQL sends back the ID immediately, whether
    the row was just inserted or already existed.

    Why do we need the ID?
    Every chunk row has a document_id foreign key pointing to its parent.
    The ID connects chunks back to their source document.
    """
    cursor.execute(
        """
        INSERT INTO documents (filename, source_type)
        VALUES (%s, %s)
        ON CONFLICT (filename) DO NOTHING
        RETURNING id
        """,
        (filename, source_type)
    )
    row = cursor.fetchone()

    if row:
        return row[0]   # freshly inserted — return new ID

    # ON CONFLICT DO NOTHING means RETURNING returns nothing on conflict.
    # We need to fetch the existing ID separately.
    cursor.execute(
        "SELECT id FROM documents WHERE filename = %s",
        (filename,)
    )
    return cursor.fetchone()[0]


# ── Chunk insertion ───────────────────────────────────────────────────────────

def insert_chunks(
    cursor,
    embedded_chunks: list[EmbeddedChunk],
    document_id: int,
) -> int:
    """
    Bulk inserts all chunks for one document.

    execute_values() — inserts all rows in one SQL statement.
    Much faster than calling cursor.execute() in a loop.
    For 86 chunks: 1 database round trip instead of 86.

    The embedding is stored as a pgvector vector type.
    register_vector() (called at connection time) handles the conversion
    from Python list → PostgreSQL vector automatically.
    """
    rows = [
        (
            document_id,
            ec.content,
            ec.return_code,
            ec.return_category,
            ec.return_window,
            ec.can_retry,
            ec.max_retries,
            ec.embedding,   # list[float] → vector(384) via registered adapter
            ec.strategy,
        )
        for ec in embedded_chunks
    ]

    execute_values(
        cursor,
        """
        INSERT INTO chunks (
            document_id,
            content,
            return_code,
            return_category,
            return_window,
            can_retry,
            max_retries,
            embedding,
            strategy
        ) VALUES %s
        """,
        rows,
    )
    return len(rows)


# ── Main ingest pipeline ──────────────────────────────────────────────────────

def ingest_source(
    filepath: str,
    source_type: str,
    strategy: str = "clause",
    embedder: Embedder | None = None,
) -> dict:
    """
    Full pipeline for one source file:
      read → chunk → embed → store

    Args:
        filepath:    Path to a .txt file in data/raw/
        source_type: Label stored in documents table ("nacha_reference" etc.)
        strategy:    "clause" or "recursive"
        embedder:    Pass an existing Embedder to reuse the loaded model.
                     If None, loads a new one (slow — 2 seconds per load).

    Returns:
        dict with counts of what was inserted.

    Transaction design:
        conn.commit() only runs after ALL chunks are inserted successfully.
        If anything fails mid-way, conn.rollback() undoes everything.
        You never end up with a half-ingested document.
    """
    filename = Path(filepath).name

    if embedder is None:
        embedder = Embedder()

    print(f"\n{'='*50}")
    print(f"Ingesting: {filename}")
    print(f"Strategy:  {strategy}")

    # Step 1 — Chunk
    print("Chunking...")
    chunks = chunk_document(
        filepath,
        source_doc=filename,
        strategy=strategy,
        is_file=True,
    )
    print(f"  → {len(chunks)} chunks")

    # Step 2 — Embed
    print("Embedding...")
    embedded = embedder.embed_chunks(chunks, show_progress=True)
    print(f"  → {len(embedded)} vectors generated")

    # Step 3 — Store
    print("Storing to PostgreSQL...")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            doc_id = insert_document(cur, filename, source_type)
            n_inserted = insert_chunks(cur, embedded, doc_id)
        conn.commit()
        print(f"  → document_id={doc_id}, {n_inserted} chunks stored")
        return {
            "filename":   filename,
            "document_id": doc_id,
            "chunks":     n_inserted,
            "strategy":   strategy,
        }
    except Exception as e:
        conn.rollback()
        print(f"  ✗ Failed: {e}")
        raise
    finally:
        conn.close()


def ingest_all_sources(strategy: str = "clause") -> list[dict]:
    """
    Ingests all four source files in data/raw/.
    Loads the embedding model once and reuses it for all files.
    """
    sources = [
        ("data/raw/achforbusiness_full_list.txt", "nacha_reference"),
        ("data/raw/plaid_return_codes.txt",        "nacha_reference"),
        ("data/raw/ramp_return_codes.txt",          "nacha_reference"),
        ("data/raw/achq_developer_docs.txt",        "nacha_reference"),
    ]

    # Load model once — reuse across all files
    embedder = Embedder()
    results = []

    for filepath, source_type in sources:
        if not Path(filepath).exists():
            print(f"Skipping {filepath} — file not found")
            continue
        result = ingest_source(filepath, source_type, strategy, embedder)
        results.append(result)

    # Summary
    print(f"\n{'='*50}")
    print("INGEST COMPLETE")
    total_chunks = sum(r["chunks"] for r in results)
    print(f"  Sources ingested: {len(results)}")
    print(f"  Total chunks:     {total_chunks}")

    return results


if __name__ == "__main__":
    ingest_all_sources(strategy="clause")