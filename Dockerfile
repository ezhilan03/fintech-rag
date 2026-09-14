FROM python:3.11-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /usr/local/bin/uv
ARG SOURCE_REVISION=unknown
ENV SOURCE_REVISION=$SOURCE_REVISION
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --only-group runtime
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH="/app" HF_HOME="/opt/hf"
COPY src/ ./src/
# Pin and bundle the tested embedding snapshot. Startup needs no model download.
RUN python -c "from src.ingestion.embedder import Embedder; Embedder()"
RUN useradd --uid 1000 --create-home appuser
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1
USER 1000:1000
EXPOSE 8000
# Reports health; restart behavior must be configured by the deployment platform.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
CMD ["uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS test
USER root
RUN uv sync --frozen --only-group runtime --group ingestion-test
COPY tests/ ./tests/
COPY eval/ ./eval/
COPY scripts/ ./scripts/
USER 1000:1000
CMD ["python", "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"]

FROM runtime AS production
