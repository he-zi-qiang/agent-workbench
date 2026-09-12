# The image for the one service that runs a browser (ADR-0112).
#
# **Why a second image rather than one more `--extra` on the shared one.**
# ADR-0107 §3.3 put the Docker CLI in the shared image and gave the reason:
# one static binary, and a CLI without a socket is inert, so every other
# container carrying it costs nothing. Neither half of that argument reaches
# here. Chromium plus the X/GTK/NSS libraries it links against is a few hundred
# megabytes, and `api`, both Task Workers, `ingestion`, `sandbox`, `encoder`
# and `browser-egress` would each carry a browser none of them can start --
# seven copies of a cost that one service incurs.
#
# So this follows `docker/sandbox-pdf.Dockerfile` instead: a separate image,
# built separately, named in `compose.yaml`. The difference from that one is
# that this image *does* derive from the project image -- it needs the
# application code and the console scripts, which the sandbox image must
# deliberately not have.
#
# `browser-egress` stays on the shared image. It runs `GuardedProxy` and
# nothing else, and that it needs no browser is the whole shape of ADR-0112
# §3.3: the container that can leave is not the container that renders.
ARG BASE_IMAGE=agent-workbench:local
FROM ${BASE_IMAGE}

# Root to install, back to `app` at the end. The base image ends as `app:app`.
USER root:root

# Where Playwright puts the browser, and it has to be said twice -- once for
# the install below and once for the process that will run it -- because the
# default is `$HOME/.cache/ms-playwright`, and the installing user (root) and
# the running user (app) do not share a home. `/opt` is outside `/app`, so the
# base image's `chown -R app:app /app` neither covers it nor needs to.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

# The wheel first, from the lock, for the same reason the base image uses
# `--frozen`: a resolver that runs here could choose a different Playwright
# than `uv.lock` records, and then the browser this installs would be the wrong
# one for the API that drives it. `--extra embedding` is repeated because
# `uv sync` makes the environment match what is asked for -- naming only
# `browser` here would *remove* the embedding runtime the base image installed.
RUN uv sync --frozen --no-dev --no-editable --extra embedding --extra browser

# `--with-deps` is an `apt-get install` of the libraries Chromium links
# against, chosen by Playwright for this exact browser build rather than by us
# from a list that goes stale. Without them the binary is present and fails at
# `dlopen` -- a failure that reads like a bug in the code that launched it.
#
# `--only-shell` is deliberately NOT used: the headless shell cannot take the
# screencast frames that ADR-0112 §3.6 sends to the console, and a page that
# renders differently from the one a person will open is not evidence about
# that page.
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /opt/ms-playwright

# The mount point `compose.yaml` binds the artifact volume to, read-only. It is
# created here so that the read-only root filesystem in the hardening anchor
# has somewhere to bind it: a bind onto a path that does not exist fails at
# start, and the message names the container rather than the missing directory.
RUN mkdir -p /workspace

USER app:app

CMD ["agent-browser-mcp"]
