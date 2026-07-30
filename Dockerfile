# The lock file, exact Python minor and non-root runtime make this image
# reproducible without baking configuration or secret material into a layer.
# Refresh the base image digest in a reviewed dependency-update PR before
# publishing outside local Compose.
FROM ghcr.io/astral-sh/uv:0.11.31 AS uv

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

# ``--frozen`` refuses a lock/source mismatch and ``--no-editable`` ensures
# the runtime starts from the built package, not a host-mounted checkout.
RUN uv sync --frozen --no-dev --no-editable \
    && mkdir -p /var/lib/agent-workbench/artifacts \
    && chown -R app:app /app /var/lib/agent-workbench

USER app:app

CMD ["agent-api"]
