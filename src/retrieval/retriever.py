# src/retrieval/retriever.py
"""
Hybrid retriever combining BM25 keyword search + pgvector vector search
fused with Reciprocal Rank Fusion (RRF).

Key improvements over naive retrieval:
  - Query-type aware weighting: explicit code queries boost BM25
  - R61-R85 excluded: dishonored/correction codes pollute results
  - Deduplication: one result per return code
  - Injection: guarantees explicitly named codes appear in results
  - Similarity threshold: drops irrelevant vector results
"""

from __future__ import annotations
import os
import re
import psycopg2
from pgvector.psycopg2 import register_vector
from rank_bm25 import BM25Okapi
from dotenv import load_dotenv
from dataclasses import dataclass
from typing import Optional

from src.ingestion.embedder import Embedder

load_dotenv()


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class RetrievalResult:
    chunk_id:        int
    content:         str
    source_doc:      str
    return_code:     Optional[str]
    return_category: Optional[str]
    return_window:   Optional[str]
    can_retry:       Optional[bool]
    max_retries:     Optional[int]
    vector_score:    float
    bm25_rank:       int
    rrf_score:       float


# ── Retriever ─────────────────────────────────────────────────────────────────

class HybridRetriever:

    def __init__(
        self,
        embedder: Optional[Embedder] = None,
        strategy: str = "clause",
        top_k: int = 5,
        rrf_k: int = 60,
        similarity_threshold: float = 0.75,
    ):
        self.top_k = top_k
        self.rrf_k = rrf_k
        self.strategy = strategy
        self.similarity_threshold = similarity_threshold
        self.embedder = embedder or Embedder()

        print(f"Loading {strategy} chunks from database for BM25 index...")
        self._load_chunks()
        print(f"Retriever ready — {len(self.chunks)} chunks indexed")

    def _get_connection(self):
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        register_vector(conn)
        return conn

    def _load_chunks(self):
        """
        Loads chunks excluding R61-R85 dishonored/correction codes.

        Why exclude R61-R85?
        These are bank-to-bank correction codes (dishonored returns,
        return-of-returns). Nobody in payment operations queries them
        directly. Having them in the corpus causes vector search to
        surface them for any question containing "return" because their
        entire content is about return processes — polluting results
        for operational codes like R01-R29.

        They remain in the database for completeness — just not searched.
        """
        conn = self._get_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.content, c.return_code,
                       c.return_category, c.return_window,
                       c.can_retry, c.max_retries, d.filename
                FROM chunks c
                JOIN documents d ON c.document_id = d.id
                WHERE c.strategy = %s
                AND (c.return_code IS NULL OR c.return_code < 'R61')
                ORDER BY c.id
                """,
                (self.strategy,)
            )
            rows = cur.fetchall()
        conn.close()

        self.chunks = rows
        tokenized = [row[1].lower().split() for row in rows]
        self.bm25 = BM25Okapi(tokenized)
        self.id_to_index = {row[0]: i for i, row in enumerate(rows)}

    def _is_explicit_code_query(self, query: str) -> bool:
        """
        Returns True if the query contains a specific return code.

        Examples:
          "can I retry R29?"          → True
          "return window for R02"     → True
          "difference R01 and R07"    → True
          "what happens unauthorized" → False

        When True, BM25 gets heavier weighting in RRF because
        exact keyword match is more reliable than semantic similarity
        for finding a specific known code.
        """
        return bool(re.search(r'\bR\d{2}\b', query, re.IGNORECASE))

    def _extract_explicit_codes(self, query: str) -> list[str]:
        """Extract all return codes explicitly mentioned in the query."""
        return list(set(re.findall(r'\bR\d{2}\b', query.upper())))

    def _inject_missing_codes(
        self,
        results: list[RetrievalResult],
        query: str,
    ) -> list[RetrievalResult]:
        """
        If the query explicitly mentions a return code absent from
        the top_k results, fetch it directly and inject at position 0.

        Must run AFTER deduplication and top_k slice so found_codes
        only reflects what the user will actually see — not deep fused
        results where R02 might exist at rank 15 but never surfaces.
        """
        explicit_codes = self._extract_explicit_codes(query)
        if not explicit_codes:
            return results

        found_codes = {r.return_code for r in results}
        missing_codes = [c for c in explicit_codes if c not in found_codes]

        if not missing_codes:
            return results

        conn = self._get_connection()
        injected = []
        with conn.cursor() as cur:
            for code in missing_codes:
                cur.execute(
                    """
                    SELECT c.id, c.content, c.return_code,
                           c.return_category, c.return_window,
                           c.can_retry, c.max_retries, d.filename
                    FROM chunks c
                    JOIN documents d ON c.document_id = d.id
                    WHERE c.strategy = %s
                    AND c.return_code = %s
                    ORDER BY length(c.content) DESC
                    LIMIT 1
                    """,
                    (self.strategy, code)
                )
                row = cur.fetchone()
                if row:
                    injected.append(RetrievalResult(
                        chunk_id        = row[0],
                        content         = row[1],
                        return_code     = row[2],
                        return_category = row[3],
                        return_window   = row[4],
                        can_retry       = row[5],
                        max_retries     = row[6],
                        source_doc      = row[7],
                        vector_score    = 1.0,
                        bm25_rank       = 0,
                        rrf_score       = 1.0,
                    ))
        conn.close()
        return (injected + results)[:self.top_k]

    def _vector_search(
        self,
        query_vector: list[float],
        fetch_k: int = 20,
    ) -> list[tuple[int, float]]:
        """
        Searches PostgreSQL using HNSW vector index.
        Excludes R61-R85 at database level — consistent with _load_chunks.
        """
        conn = self._get_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, 1 - (c.embedding <=> %s::vector) AS similarity
                FROM chunks c
                WHERE c.strategy = %s
                AND (c.return_code IS NULL OR c.return_code < 'R61')
                ORDER BY c.embedding <=> %s::vector
                LIMIT %s
                """,
                (query_vector, self.strategy, query_vector, fetch_k)
            )
            results = cur.fetchall()
        conn.close()

        return [
            (row[0], float(row[1]))
            for row in results
            if float(row[1]) >= self.similarity_threshold
        ]

    def _bm25_search(
        self,
        query: str,
        fetch_k: int = 20,
    ) -> list[tuple[int, float]]:
        tokenized_query = query.lower().split()
        scores = self.bm25.get_scores(tokenized_query)
        scored = [
            (self.chunks[i][0], float(scores[i]))
            for i in range(len(scores))
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:fetch_k]

    def _rrf_fusion(
        self,
        vector_results: list[tuple[int, float]],
        bm25_results:   list[tuple[int, float]],
        bm25_k:   int = 60,
        vector_k: int = 60,
    ) -> list[tuple[int, float]]:
        """
        Reciprocal Rank Fusion with separate k values per source.

        Lower k = higher weight for top-ranked results.

        For explicit code queries (e.g. "R07"):
          bm25_k=10   → BM25 rank 1 contributes 1/(1+10)  = 0.091
          vector_k=60 → vector rank 1 contributes 1/(1+60) = 0.016
          BM25 dominates — exact match wins

        For semantic queries:
          Both k=60 → equal weighting
        """
        rrf_scores: dict[int, float] = {}

        for rank, (chunk_id, _) in enumerate(vector_results):
            rrf_scores[chunk_id] = rrf_scores.get(chunk_id, 0.0)
            rrf_scores[chunk_id] += 1.0 / (rank + vector_k)

        for rank, (chunk_id, _) in enumerate(bm25_results):
            rrf_scores[chunk_id] = rrf_scores.get(chunk_id, 0.0)
            rrf_scores[chunk_id] += 1.0 / (rank + bm25_k)

        return sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)

    def _deduplicate(
        self,
        results: list[RetrievalResult],
    ) -> list[RetrievalResult]:
        seen_codes: dict[str, RetrievalResult] = {}
        seen_preamble = 0
        deduped = []

        for result in results:
            if result.return_code is None:
                if seen_preamble < 1:
                    deduped.append(result)
                    seen_preamble += 1
            elif result.return_code not in seen_codes:
                seen_codes[result.return_code] = result
                deduped.append(result)

        return deduped

    def retrieve(self, query: str) -> list[RetrievalResult]:
        """
        Main retrieval method.

        Pipeline:
          1. Embed query with BGE prefix
          2. Vector search (pgvector HNSW)
          3. BM25 keyword search (in-memory)
          4. RRF fusion — query-type aware weighting
          5. Deduplicate — one result per return code
          6. Slice to top_k
          7. Inject any explicitly named codes missing from top_k
        """
        query_vector = self.embedder.embed_query(query)
        vector_results = self._vector_search(query_vector, fetch_k=20)
        bm25_results   = self._bm25_search(query, fetch_k=20)

        if not vector_results and not bm25_results:
            return []

        if self._is_explicit_code_query(query):
            fused = self._rrf_fusion(
                vector_results, bm25_results,
                bm25_k=10,
                vector_k=60,
            )
        else:
            fused = self._rrf_fusion(
                vector_results, bm25_results,
                bm25_k=60,
                vector_k=60,
            )

        vector_score_map = {chunk_id: score for chunk_id, score in vector_results}
        bm25_rank_map    = {chunk_id: rank for rank, (chunk_id, _)
                            in enumerate(bm25_results)}

        results = []
        for chunk_id, rrf_score in fused:
            idx = self.id_to_index.get(chunk_id)
            if idx is None:
                continue
            row = self.chunks[idx]
            results.append(RetrievalResult(
                chunk_id        = row[0],
                content         = row[1],
                return_code     = row[2],
                return_category = row[3],
                return_window   = row[4],
                can_retry       = row[5],
                max_retries     = row[6],
                source_doc      = row[7],
                vector_score    = vector_score_map.get(chunk_id, 0.0),
                bm25_rank       = bm25_rank_map.get(chunk_id, 999),
                rrf_score       = rrf_score,
            ))

        results = self._deduplicate(results)
        results = results[:self.top_k]
        results = self._inject_missing_codes(results, query)
        return results[:self.top_k]