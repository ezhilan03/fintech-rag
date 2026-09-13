# tests/test_chunker.py
from src.ingestion.chunker import chunk_document

def test_chunker():
    # Test clause strategy on ramp — our richest source
    ramp_chunks = chunk_document(
        "data/raw/achforbusiness_full_list.txt",
        "achforbusiness_full_list",
        strategy="clause",
        is_file=True
    )

    # Test recursive on same file for comparison
    ramp_recursive = chunk_document(
        "data/raw/ramp_return_codes.txt",
        "ramp_return_codes",
        strategy="recursive",
        is_file=True
    )

    print(f"Clause chunks:    {len(ramp_chunks)}")
    print(f"Recursive chunks: {len(ramp_recursive)}")

    # Find R07 and inspect extracted metadata
    r07 = next((c for c in ramp_chunks if c.return_code == "R07"), None)
    if r07:
        print(f"\nR07 clause chunk:")
        print(f"  window:    {r07.return_window}")
        print(f"  can_retry: {r07.can_retry}")
        print(f"  retries:   {r07.max_retries}")
        print(f"  category:  {r07.return_category}")
        print(f"  preview:   {r07.content[:120]}...")
    else:
        print("\nR07 not found — check clause split regex")

    # Metadata coverage across all chunks
    has_window = sum(1 for c in ramp_chunks if c.return_window)
    has_retry  = sum(1 for c in ramp_chunks if c.can_retry is not None)
    code_chunks = sum(1 for c in ramp_chunks if c.return_code)

    print(f"\nMetadata coverage across clause chunks:")
    print(f"  chunks with return code:  {code_chunks}/{len(ramp_chunks)}")
    print(f"  return_window extracted:  {has_window}/{len(ramp_chunks)}")
    print(f"  can_retry extracted:      {has_retry}/{len(ramp_chunks)}")

    # Show first 5 chunks so we can see the split is working
    print(f"\nFirst 5 chunks:")
    for c in ramp_chunks[:5]:
        print(f"  [{c.return_code or 'preamble':>10}] "
              f"window={c.return_window or 'none':>20} "
              f"retry={str(c.can_retry):>5} "
              f"| {c.content[:60].strip()}...")

from src.ingestion.embedder import Embedder, embedding_stats

def test_embedder():
    print("\n" + "="*50)
    print("EMBEDDER TEST")
    print("="*50)

    # Get chunks from our best source
    chunks = chunk_document(
        "data/raw/achforbusiness_full_list.txt",
        "achforbusiness_full_list",
        strategy="clause",
        is_file=True
    )

    embedder = Embedder()
    embedded = embedder.embed_chunks(chunks[:10])  # just first 10 to keep it fast

    stats = embedding_stats(embedded)
    print(f"\nEmbedding stats:")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    # Spot check — R01 embedding should exist and be correct length
    r01 = next((ec for ec in embedded if ec.return_code == "R01"), None)
    if r01:
        print(f"\nR01 embedding:")
        print(f"  dimensions: {len(r01.embedding)}")
        print(f"  first 5 values: {r01.embedding[:5]}")
        print(f"  magnitude: {sum(x**2 for x in r01.embedding)**0.5:.6f}")

    # Test query embedding
    query_vec = embedder.embed_query("can I retry a payment with insufficient funds?")
    print(f"\nQuery embedding:")
    print(f"  dimensions: {len(query_vec)}")
    print(f"  magnitude: {sum(x**2 for x in query_vec)**0.5:.6f}")

from src.ingestion.ingestor import ingest_source, get_connection

def test_ingestor():
    print("\n" + "="*50)
    print("INGESTOR TEST")
    print("="*50)

    # Test connection first
    try:
        conn = get_connection()
        conn.close()
        print("Database connection: OK")
    except Exception as e:
        print(f"Database connection FAILED: {e}")
        print("Is postgres running? Try: pgstart")
        return

    # Ingest just one file first
    result = ingest_source(
        "data/raw/achforbusiness_full_list.txt",
        source_type="nacha_reference",
        strategy="clause",
    )
    print(f"\nResult: {result}")

    # Verify it's in the database
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM chunks")
        chunk_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM documents")
        doc_count = cur.fetchone()[0]

        # Spot check — find R07 in the database
        cur.execute(
            "SELECT return_code, return_window, can_retry "
            "FROM chunks WHERE return_code = 'R07' LIMIT 1"
        )
        r07_row = cur.fetchone()

    conn.close()

    print(f"\nDatabase state:")
    print(f"  documents: {doc_count}")
    print(f"  chunks:    {chunk_count}")
    if r07_row:
        print(f"  R07 row:   code={r07_row[0]} window={r07_row[1]} can_retry={r07_row[2]}")

from src.retrieval.retriever import HybridRetriever

def test_retriever():
    print("\n" + "="*50)
    print("RETRIEVER TEST")
    print("="*50)

    retriever = HybridRetriever()

    # Test 1 — semantic query, no exact code
    print("\nQuery 1: 'what happens when customer says payment not authorized'")
    results = retriever.retrieve(
        "what happens when customer says payment not authorized"
    )
    for r in results:
        print(f"  [{r.return_code or 'none':>4}] "
              f"rrf={r.rrf_score:.4f} "
              f"vec={r.vector_score:.3f} "
              f"bm25_rank={r.bm25_rank} "
              f"| {r.content[:60].strip()}...")

    # Test 2 — exact code lookup
    print("\nQuery 2: 'R29 return code'")
    results = retriever.retrieve("R29 return code")
    for r in results:
        print(f"  [{r.return_code or 'none':>4}] "
              f"rrf={r.rrf_score:.4f} "
              f"vec={r.vector_score:.3f} "
              f"bm25_rank={r.bm25_rank} "
              f"| {r.content[:60].strip()}...")

    # Test 3 — non-existent code
    print("\nQuery 3: 'R99 return code'")
    results = retriever.retrieve("R99 return code")
    print(f"  Results: {len(results)} "
          f"{'(correctly empty or low confidence)' if len(results) == 0 else ''}")
    for r in results:
        print(f"  [{r.return_code or 'none':>4}] "
              f"rrf={r.rrf_score:.4f} "
              f"vec={r.vector_score:.3f} "
              f"| {r.content[:60].strip()}...")

if __name__ == "__main__":
    test_chunker()
    test_embedder()
    test_ingestor()
    test_retriever()