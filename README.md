# Fintech Hybrid RAG Engine

A production-grade Retrieval-Augmented Generation system for querying ACH payment
compliance documentation. Built for fintech payment operations teams who need fast,
accurate, auditable answers on Nacha return codes, retry rules, and compliance thresholds.

**Live demo:** `https://fintech-rag.up.railway.app/docs`

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Indexing pipeline                       │
│                                                             │
│  Nacha/OFAC sources → Fetcher → Chunker → Embedder → pgvector │
│  (4 authoritative    (httpx +  (clause   (BGE-small  (HNSW  │
│   web sources)       BS4)      split)    -en-v1.5)   index) │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                      Query pipeline                          │
│                                                             │
│  Question → Hybrid Retriever → Prompt builder → Claude API  │
│             ├── BM25 search                   (Haiku)       │
│             ├── pgvector search                             │
│             ├── RRF fusion                   → Answer +     │
│             ├── Deduplication                  Sources +    │
│             └── Code injection                 Latency      │
└─────────────────────────────────────────────────────────────┘
```

---

## Key design decisions

### Hybrid retrieval (BM25 + dense vector + RRF)

Pure vector search misses exact code lookups — "R29" has no semantic
meaning as a string, so BGE may return R07 or R10 instead. Pure BM25
misses semantic queries — "corporate account says not authorized" doesn't
keyword-match R29's chunk reliably.

Hybrid search with Reciprocal Rank Fusion combines both:

```
BM25 result list:    [R29, R05, R10, ...]    ← exact keyword match
Vector result list:  [R10, R29, R07, ...]    ← semantic similarity

RRF score = 1/(rank_in_bm25 + k) + 1/(rank_in_vector + k)
R29 appears in both → highest combined score
```

### Query-type aware weighting

Explicit code queries ("return window for R07") get heavier BM25 weight
(`bm25_k=10` vs default `60`). This guarantees the named code appears
first regardless of vector similarity scores.

Semantic queries ("what happens when customer says unauthorized") use equal
weighting — semantic understanding matters more than keyword matching.

### Clause chunking

Nacha documentation is structured as legal clauses — one entry per return
code with its own definition, return window, and retry rules. Clause
splitting respects these natural boundaries rather than cutting at arbitrary
character counts.

Each chunk is self-contained: R07's chunk contains everything about R07.
No partial information split across boundaries.

### R61-R85 exclusion

Dishonored return codes (R61-R85) are bank-to-bank correction codes that
nobody queries in normal payment operations. Including them polluted
results because their content is dense with "return" language, scoring
high on both BM25 and vector search for any question containing "return."

They remain in the database — just excluded from the searchable index.

---

## Evaluation (RAGAS)

15-question test set covering retry eligibility, return windows, semantic
queries, threshold questions, and edge cases. Scores after retriever
improvements:

| Metric              | Clause | Recursive | Winner    |
|---------------------|--------|-----------|-----------|
| Faithfulness        | 0.829  | 0.818     | Clause    |
| Context Precision   | 0.442  | 0.582     | Recursive |
| Context Recall      | 0.603  | 0.559     | Clause    |
| **Average**         | 0.625  | 0.653     | Recursive |

**Why clause chunking is the production choice despite lower average:**

Context Recall matters more than Context Precision for compliance-critical
operational systems. Missing a return code causes Nacha violations (fines,
enforcement, potential suspension). Retrieving extra context merely
increases LLM token usage.

Clause chunking's +8% recall improvement over recursive is worth the
-14% precision cost for ACH payment operations.

**Retriever improvements made during evaluation:**
- Context Recall: 0.410 → 0.603 (+47%) via R61-R85 exclusion + injection
- Faithfulness: 0.777 → 0.829 (+7%) via cleaner corpus
- Found that recursive precision superiority comes from larger chunks
  blending adjacent context — useful for comparison queries, not operational ones

---

## Tech stack

| Layer | Technology |
|---|---|
| API framework | FastAPI |
| Vector database | PostgreSQL 16 + pgvector (HNSW index) |
| Keyword search | rank-bm25 (BM25Okapi, in-memory) |
| Embedding model | BAAI/bge-small-en-v1.5 (local, 384 dims) |
| LLM | Claude Haiku 4.5 (Anthropic API) |
| Evaluation | RAGAS 0.4.3 |
| Observability | Langfuse |
| Containerization | Docker + docker-compose |
| Deployment | GCP Cloud Run |
| Package manager | uv |

---

## Project structure

```
fintech-rag/
├── src/
│   ├── ingestion/
│   │   ├── fetcher.py      # downloads source documents
│   │   ├── chunker.py      # clause + recursive splitting
│   │   ├── embedder.py     # BGE vectorization
│   │   └── ingestor.py     # PostgreSQL storage
│   ├── retrieval/
│   │   └── retriever.py    # hybrid BM25 + vector + RRF
│   ├── api/
│   │   ├── app.py          # FastAPI endpoints
│   │   └── prompt.py       # system + query prompts
│   └── db/
│       └── schema.sql      # PostgreSQL schema
├── eval/
│   ├── test_dataset.py     # 15 ground-truth Q&A pairs
│   └── evaluator.py        # RAGAS harness
├── data/
│   └── raw/                # fetched source documents
├── tests/
│   └── test_versioned_ingestion.py # isolated PostgreSQL regression tests
├── Dockerfile
├── docker-compose.yml
└── README.md
```

---

## Quick start

### With Docker (recommended)

```bash
# Clone and configure
git clone https://github.com/ezhilan03/fintech-rag
cd fintech-rag
cp .env.example .env
# Add your ANTHROPIC_API_KEY to .env

# Start everything
docker compose up

# API is available at http://localhost:8000
# Interactive docs at http://localhost:8000/docs
```

### Local development

```bash
# Prerequisites: Python 3.11+, uv, PostgreSQL 16 + pgvector

# Install dependencies
uv sync

# Database setup
pgstart  # or: pg_ctl -D /opt/homebrew/var/postgresql@16 start
psql fintech_rag -f src/db/schema.sql

# Fetch source documents and ingest
uv run python src/ingestion/fetcher.py
uv run python -c "from src.ingestion.ingestor import ingest_all_sources; ingest_all_sources()"

# Start API
PYTHONPATH=. uv run uvicorn src.api.app:app --reload --port 8000
```

### Example query

```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"question": "can I retry an R29 return?"}'
```

```json
{
  "answer": "No. R29 (Corporate Customer Advises Not Authorized) cannot be retried without new written authorization. Retrying without new authorization is a Nacha violation.",
  "sources": [{"return_code": "R29", "can_retry": false, "return_window": "2 banking days"}],
  "retrieval_ms": 45,
  "llm_ms": 823,
  "total_ms": 868
}
```

---

## Data sources

| Source | Coverage | Last fetched |
|---|---|---|
| achforbusiness.com | R01–R85 complete list | July 2026 |
| plaid.com | Common codes + timing rules | May 2026 |
| ramp.com | Operational guidance per code | July 2026 |
| developers.achq.com | Structured table format | July 2026 |

Sources are fetched fresh via `src/ingestion/fetcher.py`. Re-run to pick
up Nacha rule changes — no code changes required.

---

## Domain expertise

Built on 2+ years of production ACH payment operations experience:

- ACH payment lifecycle (origination, settlement, returns)
- Nacha operating rules and return reason codes (R01-R85)
- Return rate thresholds (0.5% unauthorized, 3% administrative, 15% overall)
- Retry rules and unauthorized return compliance requirements
- EFT batch processing and merchant settlement operations

Domain knowledge shaped every architectural decision — from clause chunking
(matches Nacha document structure) to R61-R85 exclusion (operationally
irrelevant to payment teams) to the test dataset (real operational questions).

---

## Author

**Ezhilan Chinnasamy** — AI & Data Engineer  
[LinkedIn](https://linkedin.com/in/ezhilan-chinnasamy) · [GitHub](https://github.com/ezhilan03)


### Versioned ingestion reliability

`ingest_source` treats the filename as a stable source identity. Use `source_id=`
when unrelated vendors share a filename. It snapshots UTF-8 source bytes once,
fingerprints the source, strategy, model identifier, code and dependency lock,
and serializes writers per source using a PostgreSQL transaction lock.

An identical active revision is a no-op. An update commits its revision archive
and active chunks together; failures leave the previous revision active. A source
change retires all strategies built from its previous text. A strategy/model
change replaces that strategy. Re-ingesting a historical source reactivates its
archived revision. Missing/empty sources are errors, not implicit deletion.

`src/db/schema.sql` is additive and can be reapplied to an existing database.
Pre-existing unversioned chunks are preserved by bootstrap and archived on their
first replacement, explicitly marked as lacking verified source provenance.
No original source bytes or model identity are invented for legacy data.

Retrieval reloads the small demo corpus under a repeatable-read transaction for
each query, keeping BM25, vector search and explicit-code lookup consistent.
Queries on one retriever instance are serialized; larger-corpus caching/pooling
is future performance work. Revision history currently has no retention policy.
Model identifiers are recorded; pinning exact upstream model weights remains a
release task. This milestone does not establish answer quality or deployment.

Automated tests use deterministic test vectors and an explicitly configured,
disposable PostgreSQL 16 + pgvector database; they never call a paid model or
use `DATABASE_URL` as the test target. Run `uv sync --frozen --only-group ingestion-test`, then set `RAG_TEST_DATABASE_URL` and run
`uv run --no-sync pytest tests -q`. The same checks run in GitHub Actions.
Interactive source/model examples are in `tests/manual_ingestion_demo.py`.


### Query and citation contract

`POST /query` accepts a nonblank question of at most 2,000 characters, `strategy`
(`clause` or `recursive`), and integer `top_k` from 1–10. Both retrieval options
are honored per request. The supported model is `claude-haiku-4-5`; arbitrary
model selection and unknown request fields return 422.

Responses add `status` (`answered` or `insufficient_evidence`) and
`cited_source_ids`. Source entries have response-local citation IDs (`S1`, etc.),
chunk IDs, archived version IDs and chunk positions, source-byte hashes, and hashes
of the exact text sent as evidence. Legacy provenance fields may be null.
The context is bounded to 24,000 content characters total and 6,000 per chunk;
these are character limits, not an exact token budget. Source previews are shorter.

The answer service submits a structured tool result. Answered responses require
nonempty citation IDs that resolve to the supplied evidence and match inline
markers such as `[S1]`. Missing/unknown references, malformed results and truncated
output return 502 without returning the unverified answer. No evidence produces
a deterministic abstention without a model call. Model abstentions are normalized
to the same message. This validates references, not semantic support for each
claim or immunity to prompt injection; fixed model-quality evaluation remains open.

Provider calls have a 30-second timeout and no automatic retries. Provider timeout,
rate limit and other API failures map to 504, 503 and 502. Database failures map
to 503. Error responses omit raw provider/database details. Sync retrieval and SDK
calls execute in worker threads. `/health` checks dependency initialization and
live database access; it does not make a paid provider request.

API tests use the SDK's message schema with controlled responses, including two
conflicting evidence snippets, unknown/missing citations, malformed output,
abstention, context bounds, request validation and upstream errors. They do not
measure live model answer quality. CI runs these alongside PostgreSQL regressions.

### Fixed answer-grounding evaluation

`uv run --no-sync python -m eval.grounding` runs the offline harness replay.
`uv run --no-sync python -m eval.grounding --live` uses the existing Haiku model
and requires the app's configured `ANTHROPIC_API_KEY`. Install the locked
`ingestion-test` dependency group for either mode. Generated reports are ignored
under `eval/results/` and identify their mode, fixture/code hashes and Git revision.

The eight fixed synthetic cases cover a counterfactual retry limit, semantic
wording, comparisons, conflicting sources, absent evidence, missing facts, and
instructions embedded in both source text and a question. They exercise the real
HTTP handler/prompt/citation validator with controlled source contexts. They are
not Nacha guidance and do not measure retrieval quality. Expected answers are
never sent to the model. Replay supplies fixture answers solely to test harness
wiring; it is explicitly labelled NOT model quality.

Live mode uses no LLM judge, no automatic retries and at most one generation per
nonempty case. Before generation it counts input tokens and reserves estimated
input plus maximum output cost. The default run budget is $0.15; the harness
refuses budgets above $0.25 and retains reservations after uncertain failures.
Rates are $1/$5 per million input/output tokens for standard first-party Haiku 4.5,
checked September 13, 2026 against [Anthropic pricing](https://www.anthropic.com/claude/haiku).
This is a run-level estimate/control, not an account billing cap or tax-inclusive
invoice. Verify rates before future use. Provider usage and estimates are recorded.

Pattern and citation checks detect selected regressions. Live reports still require
human semantic review before making answer-quality claims. The old `eval/evaluator.py`
uses the earlier prompt and extra RAGAS judging calls; it is retained as historical
exploration and is not the current API release gate.

Latest live grounding evidence: [review and both runs](eval/evidence/REVIEW.md).
The initial automatic 8/8 result missed an arithmetic hallucination found during
assistant inspection. After a prompt/rubric correction, the follow-up passes 7/8
and still adds a prohibited derived calculation. This release gate is **open**;
passing API tests is not a claim that live answers are fully grounded.

The latest comparison fix passes all eight unchanged controlled-context cases.
Comparison requests (common compare/difference/versus and comparative wording)
use an extractive response: the model selects at least two distinct verbatim
excerpts, the API validates their source membership, and code renders quoted text.
The model cannot add new calculation prose in this path. Incomplete or fabricated
selections fail validation; insufficient evidence can abstain. This resolves the
observed arithmetic regression, not arbitrary paraphrase routing or semantic
completeness. See the third live run in the evidence review; prior failures remain
preserved. Full retrieval and deployment have not yet been evaluated here.
