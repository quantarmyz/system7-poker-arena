# System 7 — dev.fun Arena poker bot + real-time dashboard.
# Pure-Python deps (httpx, treys, pokerkit, openai) → slim image, no compilation.
# syntax=docker/dockerfile:1
FROM python:3.12-slim

# uv (fast resolver + runner) from the official image
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_FROZEN=1 \
    PYTHONUNBUFFERED=1 \
    S7_RUN_BACKEND=subprocess

WORKDIR /app

# 1) deps layer (cached unless pyproject/lock change)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --extra llm

# 2) app code
COPY . .

EXPOSE 8787
ENTRYPOINT ["bash", "/app/docker/entrypoint.sh"]
CMD ["dashboard"]
