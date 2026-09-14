"""Offline, non-root container checks against an explicit disposable database."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from uuid import uuid4

import httpx
import psycopg2
from psycopg2 import sql
from psycopg2.extensions import make_dsn
import torch
from eval.retrieval_check import evaluate
from src.ingestion.ingestor import apply_schema

assert os.getuid() == 1000
assert torch.version.cuda is None
assert os.environ['HF_HUB_OFFLINE'] == '1'
subprocess.run(['python','-m','pytest','tests','-q','-p','no:cacheprovider'],check=True)
retrieval = evaluate(output_dir=Path('/tmp/retrieval-results'))
assert retrieval['all_passed']
base = os.environ['RAG_TEST_DATABASE_URL']
schema = 'http_smoke_' + uuid4().hex
admin = psycopg2.connect(base)
admin.autocommit = True
server = None
created = False
try:
    with admin.cursor() as cur:
        cur.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        created = True
    dsn = make_dsn(base,options='-csearch_path='+schema+',public')
    conn = psycopg2.connect(dsn)
    try:
        with conn: apply_schema(conn)
    finally:
        conn.close()
    env = dict(os.environ,DATABASE_URL=dsn,ANTHROPIC_API_KEY='offline-placeholder-not-a-secret')
    with tempfile.TemporaryFile() as log:
        server = subprocess.Popen(['uvicorn','src.api.app:app','--host','127.0.0.1','--port','8000'],env=env,stdout=log,stderr=log)
        with httpx.Client(base_url='http://127.0.0.1:8000',timeout=10) as client:
            for _ in range(60):
                if server.poll() is not None:
                    log.seek(0)
                    raise RuntimeError('Server exited: '+log.read().decode())
                try:
                    health = client.get('/health')
                    if health.status_code == 200: break
                except httpx.TransportError:
                    pass
                time.sleep(1)
            else:
                raise TimeoutError('API readiness timeout')
            response = client.post('/query',json={'question':'What does R99 mean?'})
            assert response.status_code == 200, response.text
            assert response.json()['status'] == 'insufficient_evidence'
            assert response.json()['model_used'] is None
            assert client.post('/query',json={'question':' '}).status_code == 422
        report = {'uid':os.getuid(),'cuda':torch.version.cuda,'offline_model':True,
                  'health_status':health.status_code,'empty_query_status':response.status_code,
                  'empty_query_abstained':True,'invalid_request_status':422,'retrieval':retrieval}
        Path('/tmp/container-verification.json').write_text(json.dumps(report,indent=2))
        print(json.dumps({'container_smoke':'passed','uid':os.getuid(),'real_retrieval_cases':len(retrieval['cases'])}))
finally:
    if server:
        server.terminate()
        try: server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
    if created:
        with admin.cursor() as cur:
            cur.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
    admin.close()
