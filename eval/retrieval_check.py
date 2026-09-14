"""Run real embeddings and pgvector retrieval in a disposable schema."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4
from datetime import datetime, timezone
from eval.provenance import source_revision, dirty_worktree

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import make_dsn
from src.ingestion.embedder import Embedder
from src.ingestion.ingestor import apply_schema, ingest_source
from src.retrieval.retriever import HybridRetriever

FIXTURE = Path(__file__).parent / 'fixtures/retrieval.json'


def evaluate(output_dir=Path('eval/results')):
    base = os.environ['RAG_TEST_DATABASE_URL']  # Never infer the test target from app credentials.
    data = json.loads(FIXTURE.read_text())
    schema = 'retrieval_' + uuid4().hex
    admin = psycopg2.connect(base)
    admin.autocommit = True
    previous = os.environ.get('DATABASE_URL')
    created = False
    try:
        with admin.cursor() as cur:
            cur.execute('CREATE EXTENSION IF NOT EXISTS vector')
            cur.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            created = True
        os.environ['DATABASE_URL'] = make_dsn(base, options='-csearch_path='+schema+',public')
        conn = psycopg2.connect(os.environ['DATABASE_URL'])
        try:
            with conn: apply_schema(conn)
        finally:
            conn.close()
        embedder = Embedder()
        with tempfile.TemporaryDirectory() as directory:
            for name,text in data['documents'].items():
                path = Path(directory)/name
                path.write_text(text)
                ingest_source(str(path),'synthetic',embedder=embedder)
        retriever = HybridRetriever(embedder=embedder)
        cases = []
        for case in data['cases']:
            results = retriever.retrieve(case['question'],top_k=case['top_k'])
            found = [r.source_doc for r in results]
            passed = (not found) if case['empty'] else set(case['required_sources']) <= set(found)
            cases.append(dict(id=case['id'],passed=passed,required_sources=case['required_sources'],
                              found_sources=found,vector_scores=[r.vector_score for r in results]))
        report = {'scope':data['scope'],'embedding_model':embedder.model_name,
                  'model_revision':getattr(embedder,'revision',None),'dimensions':embedder.dimensions,
                  'fixture_sha256':hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
                  'code_sha256':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in ['src/retrieval/retriever.py','src/ingestion/embedder.py']},
                  'commit':source_revision(),
                  'working_tree_dirty':dirty_worktree(),'all_passed':all(c['passed'] for c in cases),'cases':cases}
        output_dir.mkdir(parents=True,exist_ok=True)
        path=output_dir/('retrieval_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.json')
        path.write_text(json.dumps(report,indent=2))
        print(json.dumps({'report':str(path),**report}))
        return report
    finally:
        if previous is None: os.environ.pop('DATABASE_URL',None)
        else: os.environ['DATABASE_URL']=previous
        if created:
            with admin.cursor() as cur:
                cur.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        admin.close()


if __name__ == '__main__':
    raise SystemExit(0 if evaluate()['all_passed'] else 1)
