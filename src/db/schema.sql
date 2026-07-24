-- src/db/schema.sql
-- Run this once to set up the database schema.
-- Safe to re-run — DROP IF EXISTS prevents errors.

CREATE EXTENSION IF NOT EXISTS vector;

DROP TABLE IF EXISTS chunks;
DROP TABLE IF EXISTS documents;

CREATE TABLE documents (
    id          SERIAL PRIMARY KEY,
    filename    TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL,
    ingested_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE chunks (
    id              SERIAL PRIMARY KEY,
    document_id     INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    content         TEXT NOT NULL,
    return_code     TEXT,
    return_category TEXT,
    return_window   TEXT,
    can_retry       BOOLEAN,
    max_retries     INTEGER,
    embedding       vector(384),
    strategy        TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ON chunks
USING hnsw (embedding vector_cosine_ops);