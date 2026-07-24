# src/api/app.py
"""
FastAPI application — the HTTP interface to our RAG system.

Single endpoint: POST /query
  Input:  {"question": "can I retry R29?", "strategy": "clause"}
  Output: {"answer": "...", "sources": [...], "metadata": {...}}

Why FastAPI over Flask?
  - Automatic request/response validation via Pydantic models
  - Auto-generated API docs at /docs (try it in browser after starting)
  - Native async support for production scaling
  - Type hints everywhere = self-documenting code

The retriever and embedder are initialized once at startup,
not on every request — model loading takes 2 seconds.
Loading on every request would make the API unusably slow.
"""

from __future__ import annotations
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

import anthropic
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

from src.retrieval.retriever import HybridRetriever
from src.ingestion.embedder import Embedder
from src.api.prompt import build_query_prompt, build_no_results_prompt, SYSTEM_PROMPT

load_dotenv()

# ── Global state ──────────────────────────────────────────────────────────────
# These are initialized once when the server starts.
# Storing them as module-level variables means every request
# reuses the same loaded model — not a new one per request.

retriever: Optional[HybridRetriever] = None
anthropic_client: Optional[anthropic.Anthropic] = None


# ── Startup / shutdown ────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs startup code before the server accepts requests.
    Runs shutdown code when the server stops.

    This is FastAPI's modern replacement for @app.on_event("startup").
    We load the embedding model and retriever here so they're ready
    before the first request arrives.
    """
    global retriever, anthropic_client

    print("Starting up — loading models...")
    embedder = Embedder()
    retriever = HybridRetriever(embedder=embedder)
    anthropic_client = anthropic.Anthropic(
        api_key=os.getenv("ANTHROPIC_API_KEY")
    )
    print("Ready to serve requests")

    yield  # server runs here

    print("Shutting down")


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Fintech RAG API",
    description="ACH return code query system with hybrid retrieval",
    version="0.1.0",
    lifespan=lifespan,
)


# ── Request / Response models ─────────────────────────────────────────────────
# Pydantic validates incoming JSON automatically.
# If a request is missing 'question', FastAPI returns a 422 error
# with a clear message — no manual validation needed.

class QueryRequest(BaseModel):
    question: str
    strategy: str = "clause"      # which chunk strategy to retrieve from
    top_k: int = 5                # how many chunks to retrieve
    model: str = "claude-haiku-4-5" # which Claude model to use


class SourceChunk(BaseModel):
    chunk_id:       int
    source_doc:     str
    return_code:    Optional[str]
    return_window:  Optional[str]
    can_retry:      Optional[bool]
    vector_score:   float
    rrf_score:      float
    content_preview: str          # first 200 chars — full content would bloat response


class QueryResponse(BaseModel):
    answer:         str
    sources:        list[SourceChunk]
    question:       str
    model_used:     str
    retrieval_ms:   int           # how long retrieval took
    llm_ms:         int           # how long LLM took
    total_ms:       int           # end to end latency
    chunks_found:   int


# ── Query endpoint ────────────────────────────────────────────────────────────

@app.post("/query", response_model=QueryResponse)
async def query(request: QueryRequest):
    """
    Main endpoint. Takes a question, returns an answer with sources.

    The timing fields (retrieval_ms, llm_ms) are production signals —
    they tell you where latency comes from so you know what to optimize.
    In our system retrieval should be <100ms, LLM will be 500-2000ms.
    """
    if retriever is None or anthropic_client is None:
        raise HTTPException(status_code=503, detail="Service not ready")

    start_total = time.time()

    # ── Step 1: Retrieve ───────────────────────────────────────────────────
    start_retrieval = time.time()
    results = retriever.retrieve(request.question)
    retrieval_ms = int((time.time() - start_retrieval) * 1000)

    # ── Step 2: Build prompt ───────────────────────────────────────────────
    if not results:
        # No relevant chunks found — tell LLM explicitly
        user_prompt = build_no_results_prompt(request.question)
    else:
        # Convert results to the format prompt builder expects
        chunks_for_prompt = [
            {"content": r.content, "source_doc": r.source_doc}
            for r in results
        ]
        user_prompt = build_query_prompt(request.question, chunks_for_prompt)

    # ── Step 3: LLM call ───────────────────────────────────────────────────
    start_llm = time.time()
    message = anthropic_client.messages.create(
        model=request.model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": user_prompt}
        ]
    )
    llm_ms = int((time.time() - start_llm) * 1000)
    answer = message.content[0].text

    # ── Step 4: Build response ─────────────────────────────────────────────
    total_ms = int((time.time() - start_total) * 1000)

    source_chunks = [
        SourceChunk(
            chunk_id        = r.chunk_id,
            source_doc      = r.source_doc,
            return_code     = r.return_code,
            return_window   = r.return_window,
            can_retry       = r.can_retry,
            vector_score    = r.vector_score,
            rrf_score       = r.rrf_score,
            content_preview = r.content[:200],
        )
        for r in results
    ]

    return QueryResponse(
        answer        = answer,
        sources       = source_chunks,
        question      = request.question,
        model_used    = request.model,
        retrieval_ms  = retrieval_ms,
        llm_ms        = llm_ms,
        total_ms      = total_ms,
        chunks_found  = len(results),
    )


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """
    Simple health check endpoint.
    Load balancers and monitoring tools ping this to verify the service is up.
    Returns 200 if ready, 503 if models aren't loaded yet.
    """
    if retriever is None:
        raise HTTPException(status_code=503, detail="Not ready")
    return {
        "status":        "healthy",
        "chunks_loaded": len(retriever.chunks),
        "strategy":      retriever.strategy,
    }