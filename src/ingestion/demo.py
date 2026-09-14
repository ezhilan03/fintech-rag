"""Seed the openly labelled synthetic demo and verify unchanged-source replay."""
import json
from pathlib import Path
from src.ingestion.ingestor import apply_schema, get_connection, ingest_source
from src.ingestion.embedder import Embedder


def main():
    connection = get_connection()
    try:
        with connection:
            apply_schema(connection)
    finally:
        connection.close()
    embedder = Embedder()
    files = sorted(Path('data/demo').glob('*.txt'))
    if len(files) != 6:
        raise RuntimeError('Demo corpus incomplete')
    for path in files:
        ingest_source(path, 'synthetic_demo', embedder=embedder)
    replay = [ingest_source(path, 'synthetic_demo', embedder=embedder) for path in files]
    if not all(item['replayed'] and item['chunks'] == 0 for item in replay):
        raise RuntimeError('Replay changed the corpus')
    print(json.dumps({'sources': len(files), 'unchanged_replays': len(replay)}))


if __name__ == '__main__':
    main()
