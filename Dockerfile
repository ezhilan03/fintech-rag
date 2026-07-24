# Dockerfile — single stage, simpler and more reliable

FROM python:3.11-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency files first — Docker caches this layer
# Only reinstalls when pyproject.toml or uv.lock changes
COPY pyproject.toml uv.lock* ./

# Install dependencies into .venv
RUN uv sync --frozen --no-dev

# Pre-download BGE embedding model during build
# Without this, first request takes 30+ seconds downloading 130MB
# One-liner avoids Docker misreading 'from X import Y' as a FROM instruction
RUN uv run python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5'); print('Model cached')"

# Copy application code
COPY src/ ./src/
COPY data/ ./data/

# Make venv the active Python environment
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"

EXPOSE 8000

# Health check — Docker restarts container if this fails
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
