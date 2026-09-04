"""Exit 0 when a launcher should switch the sandbox on for this start.

The container launchers -- ``docker/run-api-local.sh`` and
``docker/run-task-worker-local.sh`` -- ask this before they export
``AW_CODE__SANDBOX_ENABLED`` / ``AW_SANDBOX__ENABLED`` (ADR-0107). They have
to ask from the outside, for the reason ``decide_web_search.py`` gives about
the key: ``code.sandbox_enabled`` without a sandbox that answers is a
*startup error* by design (``SandboxSlot.open`` is fail-fast, ADR-057), so a
profile that set it statically would turn a stack whose broker could not pull
its image into a stack that does not come up -- thirty minutes into a first
run, for a condition the System page is meant to *report*.

What it probes is the broker's ``/health`` through the loopback tunnel, and
what it waits for is the runtime, not the socket: that route answers 503
until the daemon behind the broker answers ``docker version``, and 200 after.
A socket that accepts is evidence of neither. The wait is bounded because the
broker pulls its interpreter image on first start, and unbounded because
nothing else in this process has anywhere to be until the decision is made.

Plain standard library: this runs before the package has any reason to be
imported, and it is also the one file here that has to be readable by
somebody debugging a container from its log alone.
"""

from __future__ import annotations

import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

HEALTH_URL = os.environ.get("SANDBOX_HEALTH_URL", "http://127.0.0.1:8766/health")
WAIT_SECONDS = float(os.environ.get("SANDBOX_PROBE_WAIT_SECONDS", "90"))
POLL_SECONDS = 2.0


def probe() -> tuple[bool, str]:
    """One read of the broker's health. ``(runtime answers, what was seen)``."""

    try:
        with urlopen(HEALTH_URL, timeout=5) as answer:
            body = json.loads(answer.read().decode("utf-8"))
    except HTTPError as refused:
        # 503 is the broker saying it is up and the daemon is not (the sandbox
        # server's own health contract, ADR-029 §3.6).
        try:
            body = json.loads(refused.read().decode("utf-8"))
        except ValueError:
            return False, f"the sandbox broker answered {refused.code}"
        return False, (
            f"the sandbox broker answered {refused.code}: container runtime "
            f"{body.get('container_runtime', '?')} is not available"
        )
    except (URLError, OSError, ValueError) as unreachable:
        return False, f"no sandbox broker answered at {HEALTH_URL} ({unreachable})"
    if body.get("container_runtime_available") is True:
        return True, "the sandbox broker answered and its runtime is available"
    return False, "the sandbox broker answered without a container runtime"


def main() -> int:
    deadline = time.monotonic() + WAIT_SECONDS
    seen = ""
    while True:
        available, seen = probe()
        if available:
            print(f"sandbox: {seen}: sandbox_run is on for this start", file=sys.stderr)
            return 0
        if time.monotonic() >= deadline:
            break
        time.sleep(POLL_SECONDS)
    print(
        f"sandbox: {seen} after {WAIT_SECONDS:.0f}s: sandbox_run stays off for "
        "this start. The System page lists it as absent; fix the broker "
        "(docker compose logs sandbox) and restart.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":  # pragma: no cover - container entry point
    raise SystemExit(main())
