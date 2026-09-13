"""Versioned document ingestion with atomic activation and safe reruns.

Filename is the stable source identity. Supply distinct source_id values for
unrelated files with the same basename. Source disappearance is not deletion.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
from dataclasses import asdict
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values, Json
from dotenv import load_dotenv
from src.ingestion.chunker import chunk_document
from src.ingestion.embedder import Embedder

load_dotenv()


def get_connection():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def apply_schema(conn):
    with conn.cursor() as cur:
        cur.execute((Path(__file__).parents[1] / "db/schema.sql").read_text())


def ingest_source(filepath, source_type, strategy="clause", embedder=None, *, source_id=None):
    if strategy not in {"clause", "recursive"}:
        raise ValueError("Unsupported chunking strategy")
    filename = source_id or Path(filepath).name
    if not filename or not source_type:
        raise ValueError("Source identity and type are required")
    # Read once: the hash, stored source and chunker see identical bytes.
    raw = Path(filepath).read_bytes()
    text = raw.decode("utf-8")
    if not text.strip():
        raise ValueError("Empty source cannot replace an active document")
    digest = hashlib.sha256(raw).hexdigest()
    embedder = embedder or Embedder()
    pipeline = {
        "model": embedder.model_name,
        "dimensions": embedder.dimensions,
        "chunker_sha256": hashlib.sha256(Path(__file__).with_name("chunker.py").read_bytes()).hexdigest(),
        "embedder_sha256": hashlib.sha256(Path(__file__).with_name("embedder.py").read_bytes()).hexdigest(),
        "dependencies_sha256": hashlib.sha256(Path(__file__).parents[2].joinpath("uv.lock").read_bytes()).hexdigest(),
        "source_type": source_type,
        "strategy": strategy,
    }
    fingerprint = hashlib.sha256(json.dumps([digest, pipeline], sort_keys=True).encode()).hexdigest()
    conn = get_connection()
    try:
        with conn, conn.cursor() as cur:
            # One writer per source, including absent documents. Different sources
            # proceed independently; locks release on commit, rollback or disconnect.
            key = int.from_bytes(hashlib.sha256(filename.encode()).digest()[:8], "big", signed=True)
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
            cur.execute("INSERT INTO documents(filename,source_type) VALUES (%s,%s) ON CONFLICT(filename) DO NOTHING", (filename,source_type))
            cur.execute("SELECT id,content_sha256 FROM documents WHERE filename=%s FOR UPDATE", (filename,))
            doc_id, current_hash = cur.fetchone()
            cur.execute("SELECT id,chunks FROM document_versions WHERE document_id=%s AND strategy=%s AND fingerprint=%s", (doc_id,strategy,fingerprint))
            prior = cur.fetchone()
            if prior:
                version_id, snapshot = prior
                cur.execute("SELECT count(*),count(*) FILTER (WHERE version_id=%s) FROM chunks WHERE document_id=%s AND strategy=%s", (version_id,doc_id,strategy))
                total, matching = cur.fetchone()
                if current_hash == digest and total == matching == len(snapshot):
                    return dict(filename=filename, document_id=doc_id, version_id=version_id, chunks=0, strategy=strategy, replayed=True)
            else:
                chunks = chunk_document(text, source_doc=filename, strategy=strategy, is_file=False)
                if not chunks:
                    raise ValueError("Source produced no chunks")
                embedded = embedder.embed_chunks(chunks, show_progress=False)
                if len(embedded) != len(chunks):
                    raise ValueError("Incomplete embedding batch")
                snapshot = []
                for expected, ec in zip(chunks, embedded):
                    vector = [float(v) for v in ec.embedding]
                    if ec.chunk != expected or len(vector) != 384 or not all(math.isfinite(v) for v in vector) or not any(vector):
                        raise ValueError("Invalid embedding contract")
                    snapshot.append(dict(asdict(expected), embedding=vector))
                cur.execute("INSERT INTO document_versions(document_id,strategy,fingerprint,content_sha256,source_text,pipeline,chunks) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id", (doc_id,strategy,fingerprint,digest,text,Json(pipeline),Json(snapshot)))
                version_id = cur.fetchone()[0]
            # Keep an explicitly unverified legacy snapshot before replacing old
            # unversioned rows. Original raw bytes/model provenance are unknown.
            cur.execute("""INSERT INTO document_versions
                (document_id,strategy,fingerprint,content_sha256,source_text,pipeline,chunks)
                SELECT document_id,COALESCE(strategy,'legacy'), 'legacy-unversioned',
                       'unknown', string_agg(content,E'\\n' ORDER BY id),
                       '{"legacy":true,"source_bytes_verified":false}'::jsonb,
                       jsonb_agg(to_jsonb(chunks) ORDER BY id)
                FROM chunks WHERE document_id=%s AND version_id IS NULL
                GROUP BY document_id,strategy
                ON CONFLICT(document_id,strategy,fingerprint) DO NOTHING""", (doc_id,))
            # Changed source invalidates every strategy from the old source.
            # Same source replaces only the requested strategy/pipeline.
            if current_hash != digest:
                cur.execute("DELETE FROM chunks WHERE document_id=%s", (doc_id,))
            else:
                cur.execute("DELETE FROM chunks WHERE document_id=%s AND strategy=%s", (doc_id,strategy))
            rows = [(doc_id,c['content'],c['return_code'],c['return_category'],c['return_window'],c['can_retry'],c['max_retries'],c['embedding'],strategy,version_id,c['chunk_index']) for c in snapshot]
            execute_values(cur, "INSERT INTO chunks(document_id,content,return_code,return_category,return_window,can_retry,max_retries,embedding,strategy,version_id,chunk_index) VALUES %s", rows)
            cur.execute("UPDATE documents SET content_sha256=%s,source_type=%s,ingested_at=now() WHERE id=%s", (digest,source_type,doc_id))
            return dict(filename=filename, document_id=doc_id, version_id=version_id, chunks=len(rows), strategy=strategy, replayed=False)
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

    missing = [filepath for filepath, _ in sources if not Path(filepath).is_file()]
    if missing:
        raise FileNotFoundError("Missing configured source files: " + ", ".join(missing))
    conn = get_connection()
    try:
        with conn:
            apply_schema(conn)
    finally:
        conn.close()
    # Load model once — reuse across all files
    embedder = Embedder()
    results = []

    for filepath, source_type in sources:
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