# syntax=docker/dockerfile:1
# PM Agent (po_agent_strands) — see "Run with Docker" in README.md.
#
#   docker build -t po-agent .
#   docker run --rm --init -p 127.0.0.1:8000:8000 po-agent        # offline demo
#
# Two stages: `build` resolves dependencies with uv from the lock file; the final
# image holds only Python, the virtualenv and the app, and runs as a non-root
# user. Secrets are never baked in: .env is not in the build context
# (.dockerignore is an allowlist), so pass it at run time with --env-file.

ARG PYTHON_VERSION=3.13

FROM python:${PYTHON_VERSION}-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Dependencies first, from the lock file only, so editing the app doesn't
# invalidate this layer. --locked fails the build if uv.lock is stale.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev
COPY . /app

FROM python:${PYTHON_VERSION}-slim
RUN useradd --create-home --uid 10001 app
WORKDIR /app
# The code stays root-owned (read-only to the app user). Only data/ is writable:
# FY budget files, the sprint review server's exports and runs, and HOME (the
# spreadsheet lane's Microsoft token cache), so a data volume keeps all of it.
# No VOLUME line: it would leave an anonymous volume behind every run; name one
# with -v po-agent-data:/app/data instead.
COPY --from=build /app /app
RUN mkdir -p data/fy_budget/input data/fy_budget/output data/sprint-review data/home \
    && chown -R app:app data
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8000 \
    HOME=/app/data/home
USER app
EXPOSE 8000
ENTRYPOINT ["python", "scripts/container.py"]
CMD ["demo"]
