"""Exit 0 when the launcher should turn Chat's web search on for this start.

Used by ``docker/run-api-local.sh`` and nothing else. The launcher has one
decision to make before it starts the API, and it has to make it from the
outside because ``research.enabled`` without a key is a startup error rather
than a degraded start (ADR-102 §3). Since ADR-103 the console can also record
that choice itself, and a recorded choice -- either way -- takes the decision
away from this probe: the settings loader applies it, or holds it when "on"
meets no key. Only when nobody decided does "is there a usable key" stand.

Says which way it went on stderr, so ``docker compose logs api`` shows the
reason rather than only the effect.
"""

from __future__ import annotations

import sys

from agent_workbench.application.switches import SwitchRefused, SwitchStore
from agent_workbench.bootstrap.provider_key import usable_key_present
from agent_workbench.bootstrap.switches import (
    RESEARCH_SWITCH,
    launcher_decides_web_search,
    switches_file,
)


def main() -> int:
    if launcher_decides_web_search():
        print(
            "provider key present and nothing stored for research.enabled: "
            "chat web_search is on for this start",
            file=sys.stderr,
        )
        return 0
    path = switches_file()
    stored: dict[str, bool] = {}
    if path is not None:
        try:
            stored = SwitchStore(path=path, checkout_root=None).read()
        except SwitchRefused:
            stored = {}
    if RESEARCH_SWITCH in stored:
        print(
            f"research.enabled is stored as {str(stored[RESEARCH_SWITCH]).lower()} "
            "(系统 > 运行状态): the settings loader decides chat web_search",
            file=sys.stderr,
        )
    elif not usable_key_present():
        print(
            "no provider key yet: chat web_search stays off "
            "(save one in 系统 > 模型密钥, then restart)",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":  # pragma: no cover - container entry point
    raise SystemExit(main())
