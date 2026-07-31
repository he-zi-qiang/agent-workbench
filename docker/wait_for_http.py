"""One-shot bounded HTTP readiness wait used by local Compose only."""

from __future__ import annotations

import os
import time
from urllib.error import URLError
from urllib.request import urlopen

URL = os.environ.get("WAIT_FOR_HTTP_URL", "http://qdrant:6333/readyz")
DEADLINE_SECONDS = float(os.environ.get("WAIT_FOR_HTTP_DEADLINE_SECONDS", "60"))


def main() -> int:
    deadline = time.monotonic() + DEADLINE_SECONDS
    while time.monotonic() < deadline:
        try:
            with urlopen(URL, timeout=2) as response:
                if 200 <= response.status < 300:
                    return 0
        except URLError:
            pass
        time.sleep(1)
    raise SystemExit(f"timed out waiting for {URL}")


if __name__ == "__main__":  # pragma: no cover - container entry point
    main()
