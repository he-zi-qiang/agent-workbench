# The lock file, exact Python minor and non-root runtime make this image
# reproducible without baking configuration or secret material into a layer.
# Refresh the base image digest in a reviewed dependency-update PR before
# publishing outside local Compose.
FROM ghcr.io/astral-sh/uv:0.11.31 AS uv

FROM node:24.14.0-bookworm-slim@sha256:d8e448a56fc63242f70026718378bd4b00f8c82e78d20eefb199224a4d8e33d8 AS web-build

WORKDIR /build/web

RUN corepack enable \
    && corepack prepare pnpm@11.9.0 --activate

COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

COPY web ./
# The evaluation page imports the retrieval reports directly, four levels up
# from `web/src/features/evaluation/`, so they have to sit beside the web
# source at the same relative depth the compiler resolves. Without them `tsc`
# fails with TS2307 and the image cannot be built at all -- which is what it
# did between the page landing and this line.
COPY evals/rag/reports/dense-llama_index.json \
     evals/rag/reports/dense-reference.json \
     evals/rag/reports/hybrid-llama_index.json \
     evals/rag/reports/hybrid-reference.json \
     /build/evals/rag/reports/
RUN pnpm build

FROM python:3.12.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/app/.venv \
    PATH=/app/.venv/bin:$PATH

COPY --from=uv /uv /uvx /bin/

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --home-dir /app app

WORKDIR /app

COPY --chown=app:app pyproject.toml uv.lock README.md alembic.ini ./
COPY --chown=app:app config ./config
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app src ./src
COPY --chown=app:app docker ./docker
# The browser console is compiled in a disposable Node stage. Only immutable
# assets enter the Python runtime image; neither source nor node_modules does.
COPY --from=web-build --chown=app:app /build/web/dist ./web

# ``--frozen`` refuses a lock/source mismatch and ``--no-editable`` ensures
# the runtime starts from the built package, not a host-mounted checkout.
RUN uv sync --frozen --no-dev --no-editable \
    && mkdir -p /var/lib/agent-workbench/artifacts \
    && chown -R app:app /app /var/lib/agent-workbench

USER app:app

CMD ["agent-api"]
