# src/ingestion/embedder.py
"""
Converts Chunk objects into embedding vectors using a local model.

Model: BAAI/bge-small-en-v1.5
  - Runs fully local, zero API cost during development and production
  - 384 dimensions — matches our pgvector column definition
  - Strong performance on English domain-specific text
  - Downloads automatically on first run (~130MB, cached after that)

Design decisions:
  - Batch processing: embeds 32 chunks at a time, not one by one
  - L2 normalization: vectors scaled to unit length for faster HNSW indexing
  - Returns chunks with embeddings attached, not a separate list
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import numpy as np
from sentence_transformers import SentenceTransformer
from src.ingestion.chunker import Chunk


# ── EmbeddedChunk ─────────────────────────────────────────────────────────────
# Extends Chunk with an embedding field.
# We keep them separate rather than modifying Chunk directly —
# chunker.py has no business knowing about embeddings.
# Separation of concerns: chunker splits, embedder embeds, ingestor stores.

@dataclass
class EmbeddedChunk:
    chunk: Chunk                            # original chunk with all metadata
    embedding: list[float] = field(         # 384 floats from the model
        default_factory=list
    )

    # Convenience passthrough properties so callers can do
    # ec.content instead of ec.chunk.content
    @property
    def content(self):        return self.chunk.content
    @property
    def source_doc(self):     return self.chunk.source_doc
    @property
    def chunk_index(self):    return self.chunk.chunk_index
    @property
    def strategy(self):       return self.chunk.strategy
    @property
    def return_code(self):    return self.chunk.return_code
    @property
    def return_category(self):return self.chunk.return_category
    @property
    def return_window(self):  return self.chunk.return_window
    @property
    def can_retry(self):      return self.chunk.can_retry
    @property
    def max_retries(self):    return self.chunk.max_retries


# ── Embedder ──────────────────────────────────────────────────────────────────

class Embedder:
    """
    Wraps SentenceTransformer with batching and normalization.

    Why a class instead of a function?
    The model takes ~2 seconds to load from disk.
    If we used a function, it would reload the model on every call.
    A class loads it once in __init__ and reuses it for all embed() calls.
    """

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5"):
        print(f"Loading embedding model: {model_name}")
        print("(First run downloads ~130MB — cached after that)")
        self.model = SentenceTransformer(model_name)
        self.model_name = model_name
        self.dimensions = self.model.get_embedding_dimension()
        print(f"Model ready — {self.dimensions} dimensions")

    def embed_chunks(
        self,
        chunks: list[Chunk],
        batch_size: int = 32,
        show_progress: bool = True,
    ) -> list[EmbeddedChunk]:
        """
        Embeds a list of chunks in batches.

        Args:
            chunks:       List of Chunk objects from chunker.py
            batch_size:   How many chunks to embed per model call.
                          32 is a good default — large enough to be
                          efficient, small enough not to exhaust memory.
            show_progress: Show a progress bar (useful for large ingests)

        Returns:
            List of EmbeddedChunk objects with vectors attached.

        How normalization works:
            Raw vector:        [0.23, -0.87, 0.41, ...]  magnitude=1.34
            Normalized vector: [0.17, -0.65, 0.31, ...]  magnitude=1.00

            We divide each vector by its own magnitude (L2 norm).
            All vectors end up on the surface of a unit sphere.
            Cosine similarity between unit vectors = dot product.
            Dot product is faster to compute — pgvector uses it for HNSW.
        """
        if not chunks:
            return []

        # Extract just the text content for the model
        # BGE models perform better with a short instruction prefix on queries
        # For indexing (documents), no prefix needed
        texts = [chunk.content for chunk in chunks]

        # embed() handles batching internally when batch_size is passed
        # normalize_embeddings=True does L2 normalization in one step
        print(f"Embedding {len(texts)} chunks in batches of {batch_size}...")
        vectors = self.model.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=True,   # unit length — faster HNSW indexing
            show_progress_bar=show_progress,
        )

        # vectors is a numpy array shape (n_chunks, 384)
        # zip pairs each chunk with its corresponding vector row
        embedded = []
        for chunk, vector in zip(chunks, vectors):
            embedded.append(EmbeddedChunk(
                chunk=chunk,
                embedding=vector.tolist(),  # convert numpy array → plain list
                                            # plain lists serialize to JSON/SQL
            ))

        return embedded

    def embed_query(self, query: str) -> list[float]:
        """
        Embeds a single query string for retrieval.

        BGE models use a prefix for queries (not for documents).
        This asymmetry improves retrieval accuracy — the model was
        trained this way to distinguish query intent from document content.

        "Represent this sentence: " is the BGE instruction prefix.
        """
        prefixed = f"Represent this sentence: {query}"
        vector = self.model.encode(
            prefixed,
            normalize_embeddings=True,
        )
        return vector.tolist()


# ── Quick stats helper ────────────────────────────────────────────────────────

def embedding_stats(embedded_chunks: list[EmbeddedChunk]) -> dict:
    """
    Sanity check after embedding — confirms vectors look right.
    Call this after embed_chunks() to verify output before storing.
    """
    if not embedded_chunks:
        return {}

    vectors = np.array([ec.embedding for ec in embedded_chunks])

    # All normalized vectors should have magnitude very close to 1.0
    magnitudes = np.linalg.norm(vectors, axis=1)

    return {
        "count":          len(embedded_chunks),
        "dimensions":     len(embedded_chunks[0].embedding),
        "magnitude_mean": float(magnitudes.mean()),   # should be ~1.0
        "magnitude_min":  float(magnitudes.min()),    # should be ~1.0
        "magnitude_max":  float(magnitudes.max()),    # should be ~1.0
        "has_nulls":      any(
            len(ec.embedding) == 0 for ec in embedded_chunks
        ),
    }