# The sandbox image for a deployment that wants `sandbox_run` to be able to
# produce a PDF (or a chart, or a spreadsheet) rather than only text.
#
# **This is not the default and must not become it.** `DEFAULT_SANDBOX_IMAGE`
# in `apps/sandbox_mcp/executor.py` stays `python:3.12-slim`, and the comment
# above it is the reason: the sandbox needs an interpreter and nothing else,
# and a project-built image would put this repository's own code in the one
# place it must not be reachable from. Nothing from this repository is copied
# in here -- the whole file is a base image plus two packages -- so that
# objection does not reach this image; but "stock unless somebody asked" is
# still the right default, and `scripts/dev.sh` only uses this when it has
# actually been built.
#
# Why an image at all, rather than letting the script install what it needs:
# ADR-029 runs every container with `--network=none`. That is the premise the
# whole isolation argument rests on, not a setting -- so `pip install` inside a
# call cannot work, and must not be made to work. Whatever the script can
# import has to be in the image before the call starts.
#
# Base pinned by digest, the same discipline the root `Dockerfile` follows.
# Refresh it in a reviewed dependency-update PR, not in passing.
FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

# WenQuanYi Zen Hei, and the choice is load-bearing rather than a preference.
#
# ReportLab embeds TrueType outlines and **refuses PostScript/CFF ones**:
# measured 2026-08-26 against `fonts-noto-cjk`, whose `NotoSansCJK-*.ttc` are
# OTC/CFF, and reportlab raised
#
#     TTFError: TTC file ".../NotoSansCJK-Bold.ttc": postscript outlines are
#     not supported
#
# so the obvious font does not work here. wqy-zenhei is TrueType-outlined, and
# 28 MB against noto-cjk's 88 MB.
#
# A CJK font is not optional for this deployment. ADR-045 §4.3 has the measured
# failure it prevents, and the shape of that failure is why it is installed
# rather than left to the operator: exit code 0, a valid PDF, every Chinese
# character a hollow box, and every test green. A capability that fails that
# way is worse than one that is absent.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-wqy-zenhei \
 && rm -rf /var/lib/apt/lists/*

# ReportLab is BSD-licensed, which is why it rather than fpdf2 (LGPL) or
# PyMuPDF (AGPL). It is worth saying that the CI licence gate
# (`.github/workflows/ci.yml`, `pip-licenses` over an exact-string allowlist)
# does **not** see this file -- a container's contents are not this project's
# dependency tree -- so the constraint is being honoured here by choice rather
# than by enforcement. Adding a copyleft package to this image would put the
# project one `docker save` away from distributing it.
RUN pip install --no-cache-dir reportlab==4.2.5

# Nothing else. No entrypoint, no user, no working directory: the sandbox
# supplies all three per call (`--user=65534:65534`, `--tmpfs=/sandbox`, and
# the interpreter invocation), and an image that set its own would be
# describing a contract it does not own.
