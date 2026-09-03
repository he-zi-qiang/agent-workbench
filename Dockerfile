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
# `EvaluationPage.test.tsx` imports these reports directly, four levels up from
# `web/src/features/evaluation/`, so they have to sit beside the web source at
# the same relative depth the compiler resolves. Without them `tsc` fails with
# TS2307 and the image cannot be built at all -- which is what it did between
# the page landing and this line.
#
# **It is the test, not the page.** The page reads reports over HTTP; it stopped
# importing them when the API started serving the directory. The test kept the
# imports on purpose -- a fixture it made up itself would let the page drift
# from the repository, which is what the build-time import used to prevent.
# So whoever deletes that test may delete these lines; whoever reads "the page
# imports them" and goes looking will not find it.
COPY evals/rag/reports/dense-llama_index.json \
     evals/rag/reports/dense-reference.json \
     evals/rag/reports/hybrid-llama_index.json \
     evals/rag/reports/hybrid-reference.json \
     /build/evals/rag/reports/
COPY evals/chat/reports/chat-hybrid-180s.json /build/evals/chat/reports/
COPY evals/triage/reports/report.json /build/evals/triage/reports/
RUN pnpm build

FROM python:3.12.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/app/.venv \
    PATH=/app/.venv/bin:$PATH

COPY --from=uv /uv /uvx /bin/

# The layout preview (ADR-045) converts .docx to PDF with headless LibreOffice.
# Off by default, and the default is the decision rather than an oversight:
# turning it on takes the image from 1.24 GB to 1.96 GB -- measured, both ways,
# not estimated -- and adds one more several-hundred-megabyte download to every
# build that misses the layer cache, a download that does fail (it failed once
# on the day this landed). A build without it
# is not a broken build: `GET /v1/artifacts/{id}/pdf` answers 503 and the
# console falls back to the text preview, which is intact.
#
#     docker build --build-arg WITH_FIDELITY_PREVIEW=1 -t agent-workbench .
#
# ``fonts-noto-cjk`` is not optional when this is on. Without it LibreOffice
# converts Chinese documents successfully, exits zero and writes a PDF full of
# empty boxes -- no test goes red, and the only way to find out is for somebody
# to look at the page. ``libreoffice-writer`` rather than ``libreoffice``
# because this project converts Word documents and nothing else.
ARG WITH_FIDELITY_PREVIEW=0
RUN if [ "$WITH_FIDELITY_PREVIEW" = "1" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends \
            -o Acquire::Retries=3 \
            libreoffice-writer \
            fonts-noto-cjk \
        && rm -rf /var/lib/apt/lists/*; \
    fi

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --home-dir /app app

WORKDIR /app

COPY --chown=app:app pyproject.toml uv.lock README.md alembic.ini ./
COPY --chown=app:app config ./config
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app src ./src
COPY --chown=app:app docker ./docker
# `docker/run-task-worker-local.sh` gates the Worker on this probe, so it has to
# be in the image. One file rather than `scripts/`: the rest of that directory
# is the native launcher and the evaluation runners, which have no business in
# a container.
COPY --chown=app:app scripts/smoke_mcp_server.py ./scripts/
# `evaluation.reports_root` is `./evals`, read relative to the working directory
# at request time. `.dockerignore` excludes `evals/` and re-admits exactly these
# six reports for the web build; without this line they reach the Node stage and
# not the runtime, so the console's 评测 page renders an empty list on a stack
# that has the reports sitting in its own build context.
COPY --chown=app:app evals/rag/reports/dense-llama_index.json \
     evals/rag/reports/dense-reference.json \
     evals/rag/reports/hybrid-llama_index.json \
     evals/rag/reports/hybrid-reference.json \
     ./evals/rag/reports/
COPY --chown=app:app evals/chat/reports/chat-hybrid-180s.json ./evals/chat/reports/
COPY --chown=app:app evals/triage/reports/report.json ./evals/triage/reports/
# The browser console is compiled in a disposable Node stage. Only immutable
# assets enter the Python runtime image; neither source nor node_modules does.
COPY --from=web-build --chown=app:app /build/web/dist ./web

# ``--frozen`` refuses a lock/source mismatch and ``--no-editable`` ensures
# the runtime starts from the built package, not a host-mounted checkout.
#
# ``--extra embedding`` is what makes this stack the whole product rather than
# the half of it that needs no models. Without it `build_embedder` returns
# `EmbeddingUnavailable`, and then: Chat has no knowledge base, `/v1/search` is
# not registered at all, the ingestion worker writes hash vectors that no query
# can match, and every Task Worker runs ungrounded. None of that is visible in
# a browser -- the console is fast and healthy and retrieves nothing -- which is
# why it was worth the size rather than worth a footnote.
#
# The size is real and is stated where somebody meets it: `scripts/stack.cmd`
# measures the machine before it spends the time, and
# `docs/windows-quickstart.md` names the floor. The weights are NOT baked in;
# `docker/fetch_weights.py` puts them in a named volume once, and its docstring
# says why they cannot simply be downloaded on first use.
RUN uv sync --frozen --no-dev --no-editable --extra embedding \
    && mkdir -p /var/lib/agent-workbench/artifacts \
       /var/lib/agent-workbench/hf-cache \
    && chown -R app:app /app /var/lib/agent-workbench

USER app:app

CMD ["agent-api"]
