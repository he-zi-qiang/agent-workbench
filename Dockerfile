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

# The two directories the processes write are made here, with the user: the
# steps after the `USER` line below run as `app` and cannot create anything
# under `/var/lib`. The comment above that line says why they run as `app`.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --home-dir /app app \
    && mkdir -p /var/lib/agent-workbench/artifacts \
       /var/lib/agent-workbench/hf-cache \
    && chown -R app:app /var/lib/agent-workbench

WORKDIR /app

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
#
# **Two syncs, and the first one reads two files.** Until 2026-09-13 this was
# one `uv sync` after every `COPY` in this stage, so any change to `src/` or to
# the console invalidated it, and it downloaded the whole extra again: 175
# packages, torch and fourteen `nvidia-*` CUDA wheels among them. Measured on a
# rebuild whose only change was in `web/`: `Prepared 175 packages in 4m 44s`,
# the step 304.7s, then 230.3s exporting its 11.5 GB layer. Across the eleven
# rebuilds of that day the download alone ran from 4m 44s to 10m 09s, 67
# minutes in all. 5.2 GB of that layer was uv's own download cache under
# `/root/.cache/uv`, which nothing at runtime reads.
#
# So the dependencies install from `pyproject.toml` and `uv.lock` alone
# (`--no-install-project`); the project installs from what hatchling builds
# its wheel from -- `README.md` for the readme field, `config/` for the three
# files it force-includes, `src/`; and what the running stack reads but the
# wheel does not is copied after both. A change to `src/` re-runs only the
# second sync, and a change to `web/`, `docker/` or `migrations/` neither.
#
# `pyproject.toml` still invalidates the first, because it also changes for
# entry points and lint settings. Not worked around with a stub holding only
# the dependency tables: `--frozen` would trust a second description of the
# project without checking it against the first. The cache mount is for that
# case instead, and for `stack.cmd lite` flipping the LibreOffice layer above:
# uv installs from BuildKit's cache rather than the network, and the cache
# stays out of the image. The mount is another filesystem, so uv cannot
# hardlink out of it; `UV_LINK_MODE=copy` above tells it to copy instead.
#
# **The layer and the cache last only as long as BuildKit keeps them.** Docker
# Desktop keeps 20 GiB of build cache by default, and BuildKit counts the first
# sync's layer at 9.0 GB of that and the uv cache at 5.6 GB. Measured
# 2026-09-13: a build of the old layout ran alongside the first build of this
# one, the cache went past the cap, and both records were evicted within
# minutes -- the next build downloaded everything again. A rebuild that
# downloads with no lock change is that eviction, not this ordering failing;
# a machine that rebuilds all day wants `builder.gc.defaultKeepStorage` raised.
#
# **As `app`, rather than a `chown` afterwards.** The single step used to end
# in `chown -R app:app /app`, which added nothing to the layer while every
# file it touched was created in that same step. With the venv one layer down,
# the same line would copy every file it touches up into the second layer --
# the whole venv, again. Files created by their owner need no chown.
# `uid`/`gid` let `app` write the cache, and the `id` keeps it apart from
# `docker/browser.Dockerfile`'s, whose uv runs as root: root writing into this
# one would leave entries in it that `app` cannot write to.
USER app:app

COPY --chown=app:app pyproject.toml uv.lock ./
RUN --mount=type=cache,id=agent-workbench-uv-app,target=/var/cache/uv,uid=10001,gid=10001 \
    UV_CACHE_DIR=/var/cache/uv \
    uv sync --frozen --no-dev --no-editable --no-install-project --extra embedding

COPY --chown=app:app README.md ./
COPY --chown=app:app config ./config
COPY --chown=app:app src ./src
RUN --mount=type=cache,id=agent-workbench-uv-app,target=/var/cache/uv,uid=10001,gid=10001 \
    UV_CACHE_DIR=/var/cache/uv \
    uv sync --frozen --no-dev --no-editable --extra embedding

COPY --chown=app:app alembic.ini ./
COPY --chown=app:app migrations ./migrations
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

# The Docker CLI, for exactly one service (ADR-0107). `agent-sandbox-mcp`
# shells out to `docker run` once per call, and the `sandbox` service in
# compose.yaml is the one container that holds the daemon's socket. The binary
# is copied from Docker's own CLI image rather than installed from an apt
# repository: it is one static Go binary, the copy needs no network beyond the
# registry every other stage already pulls from, and it adds nothing else.
#
# It is in the shared image rather than a second one because a CLI without a
# socket is inert -- every other container here has the binary and nothing to
# point it at, which is the same as not having it. A second image would be a
# second build step for the Windows launcher to explain.
# Pinned by index digest like every other base here (resolved 2026-09-03
# through `docker buildx imagetools inspect docker:29-cli`).
COPY --from=docker:29-cli@sha256:3f4743208d2338c934d7b8bcfbe1bb54c0b2355c510ad5e0f31c0c4a54bd704e /usr/local/bin/docker /usr/local/bin/docker

# Node, for exactly one service as well: the `runner` (ADR-0115), where a
# coding session's `project_run` executes under Compose. Measured 2026-09-12:
# the first turn that had the runner looked for `node`, `deno`, `bun`, `qjs`
# and `d8` in `/usr/bin`, found none, and reported that it could not run the
# project's own `check_level.js` -- the one script the project keeps for the
# check the turn was asked to make. A shell that cannot run the project's own
# scripts is the "tool that cannot be honoured" ADR-0057 refuses.
#
# The binary alone, from the same pinned image the console is built with
# above, so this pulls nothing new. 126 MB, one file, and deliberately no
# npm: `npm install` inside a read-only container with a tmpfs home is a
# way to spend a turn on a failure the person approving cannot read, and a
# project that needs packages has its own `node_modules` in the folder the
# runner mounts. Shared image rather than a second one, for the reason the
# Docker CLI is: inert everywhere else, and one fewer build step for the
# Windows launcher to explain. `libstdc++6` and `libgcc_s1`, which it links
# against, are already in this base image (measured: `ldconfig -p`).
COPY --from=web-build /usr/local/bin/node /usr/local/bin/node

USER app:app

CMD ["agent-api"]
