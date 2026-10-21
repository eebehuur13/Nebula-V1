# syntax=docker/dockerfile:1.7
#
# Nebula: one image for both ECS services.
#   nebula serve   -> HTTP on :8000 (GET /healthz)
#   nebula worker  -> background worker
#
# Build (ARM64, as deployed on Fargate Graviton):
#   docker build --platform linux/arm64 -t nebula:dev .
#   docker build --platform linux/arm64 --build-arg BAKE_MODELS=0 -t nebula:dev .   # skip the ~2.8 GB model download
#
# Layer order: uv + system setup -> third-party deps -> models -> our code,
# so editing src/ never re-downloads models or reinstalls dependencies.

ARG PYTHON_VERSION=3.13
ARG UV_VERSION=0.9.22

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim

COPY --from=uv /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:${PATH}" \
    FASTEMBED_CACHE_PATH=/opt/models \
    HF_HUB_DISABLE_TELEMETRY=1

# Non-root runtime user. /opt/models is owned by it so baked files never need a
# separate (layer-duplicating) chown, and an unbaked image can download at runtime.
RUN groupadd --system --gid 10001 nebula \
 && useradd --system --uid 10001 --gid nebula --create-home --home-dir /home/nebula nebula \
 && mkdir -p /opt/models \
 && chown nebula:nebula /opt/models

WORKDIR /app

# 1) Third-party dependencies only (cached until pyproject.toml / uv.lock change).
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# 2) Local fallback models (cached until the dependency layer or BAKE_MODELS changes).
ARG BAKE_MODELS=1
USER nebula
RUN if [ "$BAKE_MODELS" = "1" ]; then \
      uv run --no-sync python -c "from fastembed import TextEmbedding; from fastembed.rerank.cross_encoder import TextCrossEncoder; TextEmbedding('intfloat/multilingual-e5-large', cache_dir='/opt/models'); TextCrossEncoder('BAAI/bge-reranker-v2-m3-int8', cache_dir='/opt/models')"; \
    else \
      echo "BAKE_MODELS=$BAKE_MODELS: skipping model download (fastembed will fetch into /opt/models on first use)"; \
    fi
USER root

# 3) Our code. README.md is referenced by pyproject's `readme`, so the build needs it.
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

USER nebula
EXPOSE 8000
CMD ["nebula", "serve"]
