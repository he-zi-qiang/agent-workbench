"""Exit 0 when this deployment has a provider key, used by local Compose only.

The launcher beside this file has one decision to make before it starts the API
-- whether to turn on Chat's web search -- and it has to make it from the
outside, because `research.enabled` without a key is a startup error rather than
a degraded start. A shell test would have to restate the placeholder rule the
settings validator uses; this asks the package that owns it.
"""

from __future__ import annotations

from agent_workbench.bootstrap.provider_key import usable_key_present


def main() -> int:
    return 0 if usable_key_present() else 1


if __name__ == "__main__":  # pragma: no cover - container entry point
    raise SystemExit(main())
