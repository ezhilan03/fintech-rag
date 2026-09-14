-- src/db/schema.sql
-- Run this once to set up the database schema.
-- Additive bootstrap; never discards source data.

CREATE EXTENSION IF NOT EXISTS vector;



CREATE TABLE IF NOT EXISTS documents (
    id          SERIAL PRIMARY KEY,
    filename    TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL,
    ingested_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS chunks (
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

CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON chunks
USING hnsw (embedding vector_cosine_ops);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_sha256 TEXT;
CREATE TABLE IF NOT EXISTS document_versions (
    id BIGSERIAL PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    strategy TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    source_text TEXT NOT NULL,
    pipeline JSONB NOT NULL,
    chunks JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(document_id, strategy, fingerprint)
);
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS version_id BIGINT REFERENCES document_versions(id);
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS chunk_index INTEGER;
CREATE UNIQUE INDEX IF NOT EXISTS chunks_version_position ON chunks(version_id, chunk_index);
