"""Bounded query API with checked source references and explicit abstention."""
from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import logging
import re
import time
from typing import Literal

import anthropic
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.ingestion.embedder import Embedder
from src.retrieval.retriever import HybridRetriever
from src.api.prompt import build_grounded_prompt, API_SYSTEM_PROMPT

MODEL = 'claude-haiku-4-5'
ABSTENTION = "I don't have enough information in my sources to answer this accurately."
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app):
    app.state.retriever = HybridRetriever(embedder=Embedder())
    app.state.client = anthropic.Anthropic(timeout=30.0, max_retries=0)
    try:
        yield
    finally:
        app.state.client.close()
        app.state.client = None
        app.state.retriever = None


app = FastAPI(title='Fintech RAG API', version='0.2.0', lifespan=lifespan)


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    question: str = Field(min_length=1, max_length=2000, strict=True)
    strategy: Literal['clause', 'recursive'] = 'clause'
    top_k: int = Field(default=5, ge=1, le=10, strict=True)
    model: Literal['claude-haiku-4-5'] = MODEL

    @field_validator('question')
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError('Question must not be blank')
        return value.strip()


class GroundedAnswer(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Literal['answered', 'insufficient_evidence']
    answer: str = Field(min_length=1, max_length=8000, strict=True)
    cited_source_ids: list[str] = Field(max_length=10)


class SourceChunk(BaseModel):
    citation_id: str
    chunk_id: int
    source_doc: str
    version_id: int | None
    chunk_index: int | None
    source_sha256: str | None
    context_sha256: str
    return_code: str | None
    return_window: str | None
    can_retry: bool | None
    vector_score: float
    rrf_score: float
    content_preview: str


class QueryResponse(BaseModel):
    answer: str
    status: Literal['answered', 'insufficient_evidence']
    cited_source_ids: list[str]
    sources: list[SourceChunk]
    question: str
    model_used: str | None
    retrieval_ms: int
    llm_ms: int
    total_ms: int
    chunks_found: int


def checked_answer(message, allowed):
    if message.stop_reason != 'tool_use' or len(message.content) != 1:
        raise ValueError('Expected one complete structured answer')
    block = message.content[0]
    if block.type != 'tool_use' or block.name != 'submit_answer':
        raise ValueError('Unexpected output block')
    answer = GroundedAnswer.model_validate(block.input)
    if answer.status == 'insufficient_evidence':
        if answer.cited_source_ids:
            raise ValueError('Abstention must not claim citations')
        answer.answer = ABSTENTION
        return answer
    refs = answer.cited_source_ids
    markers = set(re.findall(r'\[(S[^\]\r\n]*)\]', answer.answer, flags=re.IGNORECASE))
    if (not answer.answer.strip() or not refs or len(set(refs)) != len(refs)
            or not set(refs) <= allowed or markers != set(refs)):
        raise ValueError('Citations must identify the supplied evidence')
    return answer


def dependencies(request):
    retriever = getattr(request.app.state, 'retriever', None)
    client = getattr(request.app.state, 'client', None)
    if retriever is None or client is None:
        raise HTTPException(503, 'Service not ready')
    return retriever, client


@app.post('/query', response_model=QueryResponse)
def query(payload: QueryRequest, request: Request):
    # Sync SDK/SQL/model work runs in FastAPI's worker pool, not the event loop.
    retriever, client = dependencies(request)
    started = time.monotonic()
    try:
        results = retriever.retrieve(payload.question, strategy=payload.strategy, top_k=payload.top_k)
    except Exception:
        logger.warning('retrieval_failed')
        raise HTTPException(503, 'Retrieval unavailable') from None
    retrieval_ms = int((time.monotonic() - started) * 1000)
    context, sources = [], []
    remaining = 24000  # Character budget; not claimed to be an exact token count.
    for result in results[:payload.top_k]:
        content = result.content[:min(6000, remaining)]
        if not content.strip():
            continue
        citation_id = f'S{len(sources)+1}'
        context.append(dict(citation_id=citation_id, source_doc=result.source_doc, content=content))
        sources.append(SourceChunk(
            citation_id=citation_id, chunk_id=result.chunk_id, source_doc=result.source_doc,
            version_id=result.version_id, chunk_index=result.chunk_index, source_sha256=result.source_sha256,
            context_sha256=hashlib.sha256(content.encode()).hexdigest(),
            return_code=result.return_code, return_window=result.return_window,
            can_retry=result.can_retry, vector_score=result.vector_score,
            rrf_score=result.rrf_score, content_preview=content[:200]))
        remaining -= len(content)
        if remaining <= 0:
            break
    llm_ms = 0
    model_used = None
    answer = GroundedAnswer(status='insufficient_evidence', answer=ABSTENTION, cited_source_ids=[])
    if sources:
        model_started = time.monotonic()
        try:
            message = client.messages.create(
                model=MODEL, max_tokens=1600, system=API_SYSTEM_PROMPT,
                messages=[{'role': 'user', 'content': build_grounded_prompt(payload.question, context)}],
                tools=[{'name': 'submit_answer', 'description': 'Return a grounded answer or abstain.',
                        'input_schema': GroundedAnswer.model_json_schema()}],
                tool_choice={'type': 'tool', 'name': 'submit_answer'})
        except anthropic.APITimeoutError:
            raise HTTPException(504, 'Answer service timed out') from None
        except anthropic.RateLimitError:
            raise HTTPException(503, 'Answer service temporarily unavailable') from None
        except anthropic.APIError:
            logger.warning('answer_service_failed')
            raise HTTPException(502, 'Answer service unavailable') from None
        try:
            answer = checked_answer(message, {s.citation_id for s in sources})
        except (ValueError, ValidationError, AttributeError, TypeError):
            logger.warning('answer_contract_failed')
            raise HTTPException(502, 'Answer failed evidence validation') from None
        llm_ms = int((time.monotonic() - model_started) * 1000)
        model_used = MODEL
    return QueryResponse(
        answer=answer.answer, status=answer.status, cited_source_ids=answer.cited_source_ids,
        sources=sources, question=payload.question, model_used=model_used,
        retrieval_ms=retrieval_ms, llm_ms=llm_ms,
        total_ms=int((time.monotonic()-started)*1000), chunks_found=len(sources))


@app.get('/health')
def health(request: Request):
    retriever, _ = dependencies(request)
    try:
        count = retriever.check_ready()
    except Exception:
        raise HTTPException(503, 'Database not ready') from None
    return {'status': 'healthy', 'chunks_available': count}
