"""Real PostgreSQL/pgvector tests, isolated per schema; no model/API calls."""
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import make_dsn
import pytest

from src.ingestion.embedder import EmbeddedChunk
from src.ingestion.ingestor import ingest_source, apply_schema
from src.retrieval.retriever import HybridRetriever


class FakeEmbedder:
    model_name = 'deterministic-test-vector'
    dimensions = 384
    def __init__(self):
        self.calls = 0
        self.fail = False
    def embed_chunks(self, chunks, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError('injected embedding outage')
        return [EmbeddedChunk(c, [1.0] + [0.0] * 383) for c in chunks]
    def embed_query(self, query):
        return [1.0] + [0.0] * 383


@pytest.fixture
def db(tmp_path, monkeypatch):
    dsn = os.environ.get('RAG_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('Set RAG_TEST_DATABASE_URL to a disposable pgvector database')
    schema = 'test_' + uuid4().hex
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute('CREATE EXTENSION IF NOT EXISTS vector')
        cur.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    monkeypatch.setenv('DATABASE_URL', make_dsn(dsn, options='-csearch_path='+schema+',public'))
    conn = psycopg2.connect(os.environ['DATABASE_URL'])
    with conn:
        apply_schema(conn)
    source = tmp_path / 'source.txt'
    source.write_text('R01 Insufficient Funds\nOriginal synthetic rule with enough words for retrieval.')
    yield conn, source, FakeEmbedder()
    conn.close()
    with admin.cursor() as cur:
        cur.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
    admin.close()


def rows(conn, query):
    with conn, conn.cursor() as cur:
        cur.execute(query)
        return cur.fetchall()


def ingest(db, **kwargs):
    _, source, embedder = db
    return ingest_source(str(source), 'synthetic', embedder=embedder, **kwargs)


def test_replay_preserves_ids_and_does_not_embed(db):
    conn, _, embedder = db
    first = ingest(db)
    before = rows(conn, 'SELECT id,content,version_id FROM chunks ORDER BY id')
    second = ingest(db)
    assert second['replayed'] and second['chunks'] == 0
    assert second['version_id'] == first['version_id']
    assert embedder.calls == 1
    assert rows(conn, 'SELECT id,content,version_id FROM chunks ORDER BY id') == before


def test_update_history_and_strategy_invalidation(db):
    conn, source, _ = db
    old = ingest(db)
    ingest(db, strategy='recursive')
    original = source.read_text()
    source.write_text('R01 Insufficient Funds\nChanged synthetic rule with enough words for retrieval.')
    new = ingest(db)
    assert new['version_id'] != old['version_id']
    assert rows(conn, 'SELECT DISTINCT strategy FROM chunks') == [('clause',)]
    assert rows(conn, 'SELECT count(*) FROM document_versions') == [(3,)]
    assert all('Changed' in r[0] for r in rows(conn, 'SELECT content FROM chunks'))
    source.write_text(original)
    restored = ingest(db)
    assert restored['version_id'] == old['version_id']
    assert rows(conn, 'SELECT count(*) FROM document_versions') == [(3,)]


def test_embedding_failure_preserves_active_revision(db):
    conn, source, embedder = db
    ingest(db)
    before = rows(conn, 'SELECT id,content FROM chunks')
    source.write_text('R01 Insufficient Funds\nChanged synthetic source with enough words for chunking.')
    embedder.fail = True
    with pytest.raises(RuntimeError):
        ingest(db)
    assert rows(conn, 'SELECT id,content FROM chunks') == before
    assert rows(conn, 'SELECT count(*) FROM document_versions') == [(1,)]


def test_database_failure_rolls_back_activation(db):
    conn, source, _ = db
    ingest(db)
    before = rows(conn, 'SELECT id,content FROM chunks')
    with conn, conn.cursor() as cur:
        cur.execute("ALTER TABLE chunks ADD CONSTRAINT reject_test CHECK(content NOT LIKE '%REJECT%')")
    source.write_text('R01 Insufficient Funds\nREJECT replacement synthetic rule.')
    with pytest.raises(psycopg2.IntegrityError):
        ingest(db)
    assert rows(conn, 'SELECT id,content FROM chunks') == before
    assert rows(conn, 'SELECT count(*) FROM document_versions') == [(1,)]


def test_concurrent_replay_has_one_revision(db):
    conn, _, _ = db
    barrier = threading.Barrier(2)
    def worker():
        barrier.wait()
        return ingest(db)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))
    assert sum(r['replayed'] for r in results) == 1
    assert rows(conn, 'SELECT count(*) FROM document_versions') == [(1,)]
    assert rows(conn, 'SELECT count(*) FROM chunks') == [(results[0]['chunks'] + results[1]['chunks'],)]


@pytest.mark.parametrize('bad', ['empty', 'short', 'nan', 'zero'])
def test_invalid_source_or_embedding_cannot_replace(db, bad):
    conn, source, embedder = db
    ingest(db)
    before = rows(conn, 'SELECT id,content FROM chunks')
    source.write_text('' if bad == 'empty' else 'R01 Insufficient Funds\nInvalid replacement synthetic source with enough words for chunking.')
    if bad != 'empty':
        vector = {'short': [1.0], 'nan': [float('nan')]*384, 'zero': [0.0]*384}[bad]
        embedder.embed_chunks = lambda chunks, **kw: [EmbeddedChunk(c, vector) for c in chunks]
    with pytest.raises(ValueError):
        ingest(db)
    assert rows(conn, 'SELECT id,content FROM chunks') == before


def test_schema_rerun_preserves_data(db):
    conn, _, _ = db
    ingest(db)
    before = rows(conn, 'SELECT id,content FROM chunks')
    with conn:
        apply_schema(conn)
    assert rows(conn, 'SELECT id,content FROM chunks') == before


def test_retriever_refreshes_and_handles_empty_corpus(db):
    _, source, embedder = db
    retriever = HybridRetriever(embedder=embedder)
    assert retriever.retrieve('R01') == []
    ingest(db)
    assert any('Original' in r.content for r in retriever.retrieve('R01'))
    source.write_text('R01 Insufficient Funds\nChanged synthetic rule with enough words for retrieval.')
    ingest(db)
    results = retriever.retrieve('R01')
    assert results and all('Original' not in r.content for r in results)


def test_query_snapshot_survives_concurrent_activation(db, monkeypatch):
    _, source, embedder = db
    ingest(db)
    retriever = HybridRetriever(embedder=embedder)
    original = retriever._vector_search
    def update_then_search(*args, **kwargs):
        source.write_text('R01 Insufficient Funds\nChanged rule during query.')
        ingest(db)
        return original(*args, **kwargs)
    monkeypatch.setattr(retriever, '_vector_search', update_then_search)
    results = retriever.retrieve('R01')
    assert results and all('Original' in r.content for r in results)
    monkeypatch.setattr(retriever, '_vector_search', original)
    assert all('Original' not in r.content for r in retriever.retrieve('R01'))


def test_legacy_rows_survive_bootstrap_and_are_archived_on_update(db):
    conn, _, _ = db
    with conn, conn.cursor() as cur:
        cur.execute("INSERT INTO documents(filename,source_type) VALUES ('source.txt','legacy') RETURNING id")
        doc_id = cur.fetchone()[0]
        cur.execute("INSERT INTO chunks(document_id,content,strategy) VALUES (%s,'Legacy content without verified provenance','clause')", (doc_id,))
    with conn:
        apply_schema(conn)
    assert rows(conn, 'SELECT count(*) FROM chunks') == [(1,)]
    ingest(db)
    assert rows(conn, "SELECT pipeline->>'source_bytes_verified' FROM document_versions WHERE fingerprint='legacy-unversioned'") == [('false',)]
    assert rows(conn, "SELECT source_text FROM document_versions WHERE fingerprint='legacy-unversioned'") == [('Legacy content without verified provenance',)]
    assert rows(conn, 'SELECT count(*) FROM chunks WHERE version_id IS NULL') == [(0,)]


def test_model_identity_change_is_a_new_revision(db):
    conn, _, embedder = db
    old = ingest(db)
    embedder.model_name = 'changed-test-model'
    new = ingest(db)
    assert new['version_id'] != old['version_id']
    assert rows(conn, 'SELECT count(*) FROM document_versions') == [(2,)]
    assert rows(conn, 'SELECT DISTINCT version_id FROM chunks') == [(new['version_id'],)]


def test_explicit_source_identity_separates_same_basename(db):
    conn, _, _ = db
    ingest(db, source_id='vendor-a/source.txt')
    ingest(db, source_id='vendor-b/source.txt')
    assert rows(conn, 'SELECT count(*) FROM documents') == [(2,)]


def test_first_failure_leaves_no_document(db):
    conn, _, embedder = db
    embedder.fail = True
    with pytest.raises(RuntimeError):
        ingest(db)
    assert rows(conn, 'SELECT count(*) FROM documents') == [(0,)]


def test_request_options_provenance_and_default_isolation(db):
    _, source, embedder = db
    ingest(db)
    recursive = ingest(db, strategy='recursive')
    retriever = HybridRetriever(embedder=embedder)
    results = retriever.retrieve('R01', strategy='recursive', top_k=1)
    assert len(results) == 1
    assert results[0].version_id == recursive['version_id']
    assert results[0].chunk_index is not None
    import hashlib
    assert results[0].source_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert retriever.strategy == 'clause' and retriever.top_k == 5
    assert retriever.retrieve('R01')[0].version_id != recursive['version_id']


def test_conflicting_vendors_survive_deduplication(db):
    _, source, embedder = db
    ingest(db,source_id='vendor-a.txt')
    source.write_text('R01 Insufficient Funds\nAnother vendor gives a conflicting synthetic review rule.')
    ingest(db,source_id='vendor-b.txt')
    retriever = HybridRetriever(embedder=embedder)
    assert {r.source_doc for r in retriever.retrieve('R01')} == {'vendor-a.txt','vendor-b.txt'}


def test_unknown_explicit_code_does_not_return_different_codes(db):
    _, _, embedder = db
    ingest(db)
    retriever = HybridRetriever(embedder=embedder)
    assert retriever.retrieve('What does R99 mean?') == []


def test_zero_keyword_scores_are_not_evidence(db, monkeypatch):
    _, _, embedder = db
    ingest(db)
    retriever = HybridRetriever(embedder=embedder)
    monkeypatch.setattr(retriever,'_vector_search',lambda *a,**kw: [])
    assert retriever.retrieve('photosynthesis oak leaves') == []
