"""SSM-only synthetic demo verification. Secrets never enter reports or stdout."""
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request


def run(*args, input=None):
    return subprocess.run(args, input=input, text=True, capture_output=True, check=True).stdout.strip()


def validate_manifest(manifest, revision, repository):
    if not re.fullmatch(r'[0-9a-f]{40}', revision) or manifest.get('revision') != revision:
        raise ValueError('Invalid release revision')
    digest = manifest.get('digest', '')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('Invalid image digest')
    return repository + '@' + digest


def main():
    revision, bucket, repository, region, parameter, log_group = sys.argv[1:]
    root = Path('/var/lib/fintech-rag')
    os.umask(0o077)
    lock = (root / 'lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    report = {'revision': revision, 'synthetic_data': True, 'checks': {}, 'passed': False}
    def aws(*args):
        return run('aws', '--region', region, *args)
    def sql(statement, database='rag'):
        return run('docker', 'exec', 'rag-db', 'psql', '-U', 'postgres', '-d', database, '-Atc', statement)
    def health():
        try:
            with urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=10) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code
        except (OSError, TimeoutError):
            return 0
    def wait_health():
        for _ in range(90):
            if health() == 200:
                return
            time.sleep(2)
        raise RuntimeError('Health timeout')
    try:
        manifest = json.loads(aws('s3', 'cp', f's3://{bucket}/releases/{revision}/manifest.json', '-'))
        image = validate_manifest(manifest, revision, repository)
        report['image'] = image
        run('docker', 'login', '--username', 'AWS', '--password-stdin', repository.split('/')[0], input=aws('ecr', 'get-login-password'))
        run('docker', 'pull', image)
        run('docker', 'pull', 'pgvector/pgvector:pg16')
        if not subprocess.run(['docker', 'network', 'inspect', 'rag'], capture_output=True).returncode == 0:
            run('docker', 'network', 'create', 'rag')
        password_file = root / 'db-password'
        if not password_file.exists():
            password_file.write_text(secrets.token_hex(24))
        password = password_file.read_text().strip()
        db_env = root / 'db.env'
        db_env.write_text(f'POSTGRES_PASSWORD={password}\nPOSTGRES_DB=rag\n')
        # Dedicated names/volume: never touch any other project's database.
        subprocess.run(['docker', 'rm', '-f', 'rag-api'], capture_output=True)
        subprocess.run(['docker', 'rm', '-f', 'rag-db'], capture_output=True)
        run('docker', 'run', '-d', '--name', 'rag-db', '--network', 'rag', '--memory', '300m', '--env-file', str(db_env), '-v', 'rag-data:/var/lib/postgresql/data', 'pgvector/pgvector:pg16')
        for _ in range(60):
            if subprocess.run(['docker', 'exec', 'rag-db', 'pg_isready', '-U', 'postgres'], capture_output=True).returncode == 0:
                break
            time.sleep(2)
        sql('CREATE EXTENSION IF NOT EXISTS vector')
        # Hex-only generated password; app role has no superuser or role/database creation rights.
        sql(f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='ragapp') THEN CREATE ROLE ragapp LOGIN PASSWORD '{password}'; END IF; END $$; GRANT ALL ON SCHEMA public TO ragapp;")
        key = json.loads(aws('ssm', 'get-parameter', '--name', parameter, '--with-decryption'))['Parameter']['Value']
        env = root / 'app.env'
        env.write_text(f'DATABASE_URL=postgresql://ragapp:{password}@rag-db:5432/rag\nANTHROPIC_API_KEY={key}\nOMP_NUM_THREADS=1\nTOKENIZERS_PARALLELISM=false\n')
        del key
        seed = run('docker', 'run', '--rm', '--network', 'rag', '--memory', '1100m', '--env-file', str(env), image, 'python', '-m', 'src.ingestion.demo')
        report['checks']['seed_replay'] = json.loads(seed.splitlines()[-1])
        run('docker', 'run', '-d', '--name', 'rag-api', '--network', 'rag', '--memory', '1100m', '--env-file', str(env), '-p', '127.0.0.1:8000:8000', '--log-driver', 'awslogs', '--log-opt', f'awslogs-region={region}', '--log-opt', f'awslogs-group={log_group}', '--log-opt', f'awslogs-stream={revision}', image)
        wait_health()
        report['checks']['health'] = 200
        def query(question):
            request = urllib.request.Request('http://127.0.0.1:8000/query', data=json.dumps({'question': question}).encode(), headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        abstention = query('What is the policy for R99?')
        assert abstention['status'] == 'insufficient_evidence'
        report['checks']['unsupported_code_abstains'] = True
        comparison = query('Compare the R01 retry windows in the Alpha and Beta policies.')
        assert comparison['status'] == 'answered' and len(comparison['cited_source_ids']) >= 2
        report['checks']['live_grounded_comparison'] = comparison
        backup = root / 'backup.dump'
        with backup.open('wb') as output:
            subprocess.run(['docker', 'exec', 'rag-db', 'pg_dump', '-U', 'postgres', '-d', 'rag', '-Fc'], stdout=output, stderr=subprocess.PIPE, check=True)
        aws('s3', 'cp', str(backup), f's3://{bucket}/backups/{revision}.dump')
        sql('DROP DATABASE IF EXISTS rag_restore', 'postgres')
        sql('CREATE DATABASE rag_restore', 'postgres')
        with backup.open('rb') as source:
            subprocess.run(['docker', 'exec', '-i', 'rag-db', 'pg_restore', '-U', 'postgres', '-d', 'rag_restore', '--no-owner', '--exit-on-error'], stdin=source, capture_output=True, check=True)
        counts = 'SELECT (SELECT count(*) FROM documents), (SELECT count(*) FROM document_versions), (SELECT count(*) FROM chunks)'
        assert sql(counts) == sql(counts, 'rag_restore')
        # Check actual vector/text/version contents, not just row counts.
        checksum = "SELECT md5(string_agg(row_to_json(t)::text, '' ORDER BY id)) FROM chunks t"
        assert sql(checksum) == sql(checksum, 'rag_restore')
        report['checks']['backup_restore'] = {'counts': sql(counts), 'chunk_checksum_matches': True}
        sql('DROP DATABASE rag_restore', 'postgres')
        run('docker', 'stop', 'rag-db')
        assert health() == 503
        report['checks']['dependency_failure_detected'] = True
        run('docker', 'start', 'rag-db')
        wait_health()
        report['checks']['recovered'] = True
        report['passed'] = True
    except Exception as error:
        report['error_type'] = type(error).__name__
    finally:
        for name in ['rag-api', 'rag-db']:
            subprocess.run(['docker', 'stop', name], capture_output=True)
        for name in ['app.env', 'db.env']:
            (root / name).unlink(missing_ok=True)
        aws('cloudwatch', 'put-metric-data', '--namespace', 'Portfolio/FintechRAG', '--metric-data', json.dumps([{'MetricName':'DemoSuccess','Value':int(report['passed']),'Unit':'Count'}]))
        path = root / 'report.json'
        path.write_text(json.dumps(report, indent=2))
        aws('s3', 'cp', str(path), f's3://{bucket}/reports/{revision}.json')
        print(json.dumps(report))
    if not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
