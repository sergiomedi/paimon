# syntax=docker/dockerfile:1.7
#
# Multi-stage build. The builder resolves dependencies and produces a virtual
# environment; the runtime image carries that environment and the source, and
# nothing else — no uv, no compiler, no build cache.

FROM python:3.13-slim-bookworm AS builder

# uv is copied from its published image rather than installed with a script, so
# the version is pinned and the build makes no network call to fetch it.
COPY --from=ghcr.io/astral-sh/uv:0.12.8 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies are installed before the source is copied, so that editing a
# module does not invalidate the dependency layer.
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
# The azure extra, not just the base dependencies. It carries azure-identity,
# which is what every keyless path in this platform runs on: model calls,
# search calls, and since ADR-0038 the database connection itself. It is an
# extra rather than a dependency because a laptop running against Ollama and a
# local PostgreSQL has no use for it — and a deployed image that inherited that
# default would fail to authenticate to everything, at once, on first request.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra azure --no-install-project

COPY backend/src ./src
COPY backend/README.md ./
# The schema's history travels with the code that assumes it. A runtime image
# without migrations means the only thing that can migrate a database is a
# developer's checkout, and since ADR-0038 there is no route from a checkout to
# the database at all — the migration runs as a job, from this image.
COPY backend/alembic.ini ./
COPY backend/migrations ./migrations
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra azure


FROM python:3.13-slim-bookworm AS runtime

# An unprivileged user: a container that runs as root turns a code-execution bug
# into a container-escape attempt.
RUN groupadd --system paimon \
    && useradd --system --gid paimon --home-dir /app --no-create-home paimon

WORKDIR /app

COPY --from=builder --chown=paimon:paimon /app/.venv /app/.venv
COPY --from=builder --chown=paimon:paimon /app/src /app/src
COPY --from=builder --chown=paimon:paimon /app/alembic.ini /app/alembic.ini
COPY --from=builder --chown=paimon:paimon /app/migrations /app/migrations

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER paimon
EXPOSE 8000

# Liveness only. Readiness is the orchestrator's business: a container that kills
# itself because the database is down does not fix the database.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live', timeout=2).status == 200 else 1)"]

CMD ["uvicorn", "paimon.interfaces.api.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]
