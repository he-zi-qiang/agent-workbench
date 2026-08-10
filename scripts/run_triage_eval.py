"""Measure the triage classifier against a labelled gold set (ADR-036).

`triage.enabled` defaults to off, and this script is the reason: a deployment
turns it on after reading its own model's numbers here, not before. The gold
set has three classes -- clearly research, clearly general, and genuinely
ambiguous -- because the failure modes differ: a research/general swap runs
the wrong pipeline, while an "unsure" miss either nags a submitter who was
clear or silently guesses where it should have asked.

Scoring is deterministic: the expected label is compared against the triage
result's status/graph, nothing is judged by another model, and the gold file
is fingerprinted into the report so two reports that disagree can be checked
for having read the same questions.

Run locally with a provider key. CI never runs this; it calls a real model.

    AW_SECRETS__DEEPSEEK_API_KEY=sk-... \\
    python scripts/run_triage_eval.py

Writes evals/triage/reports/report.json and prints a per-class summary.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx

from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.memory.event_log import InMemoryEventLog
from agent_workbench.adapters.models.deepseek import DeepSeekModel, DeepSeekProfile
from agent_workbench.adapters.policy.envelope import EnvelopePolicyEngine
from agent_workbench.adapters.tools import StaticToolRegistry
from agent_workbench.application.task_triage import TaskTriageService, TriageResult
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.ports.event_log import EventScope
from agent_workbench.runtime import ClaudeLikeAgentRuntime, ToolGateway

GOLD_PATH = Path(__file__).resolve().parents[1] / "evals" / "triage" / "gold.jsonl"
REPORT_PATH = (
    Path(__file__).resolve().parents[1] / "evals" / "triage" / "reports" / "report.json"
)

EXPECTED_LABELS = {"research", "general", "unsure"}


@dataclass(frozen=True, slots=True)
class GoldCase:
    objective: str
    expected: str


def _load_gold() -> tuple[tuple[GoldCase, ...], str]:
    raw = GOLD_PATH.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()[:16]
    cases: list[GoldCase] = []
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        entry = json.loads(line)
        if set(entry) != {"objective", "expected"}:
            raise SystemExit(
                f"gold line {line_number}: keys must be objective/expected"
            )
        if entry["expected"] not in EXPECTED_LABELS:
            raise SystemExit(f"gold line {line_number}: bad label {entry['expected']}")
        cases.append(GoldCase(objective=entry["objective"], expected=entry["expected"]))
    return tuple(cases), digest


def _observed(result: TriageResult) -> str:
    """Collapse a TriageResult to the gold vocabulary.

    `default` is counted as its own failure class rather than folded into a
    guess: a run that timed out or answered unreadably did not classify, and a
    report that hid that would overstate whichever class absorbed it.
    """

    if result.status == "ask":
        return "unsure"
    if result.status == "decided" and result.graph is not None:
        return result.graph
    return "default"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", default="deepseek-chat")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    arguments = parser.parse_args()

    api_key = os.environ.get("AW_SECRETS__DEEPSEEK_API_KEY")
    if not api_key:
        print("AW_SECRETS__DEEPSEEK_API_KEY is required", file=sys.stderr)
        return 2

    cases, digest = _load_gold()
    async with httpx.AsyncClient(timeout=arguments.timeout_seconds) as http:
        # Temperature nailed to zero: a classifier eval that sampled would
        # report the sampler as often as the prompt.
        profile = DeepSeekProfile(
            model_id=arguments.model_id,
            temperature=0.0,
            timeout_seconds=arguments.timeout_seconds,
        )
        model = DeepSeekModel(
            client=http,
            api_key=api_key,
            base_url=os.environ.get("AW_MODEL__BASE_URL", "https://api.deepseek.com"),
            profiles={"main": profile, "compact": profile},
        )
        empty = StaticToolRegistry([])
        service = TaskTriageService(
            executor=ClaudeLikeAgentRuntime(
                model=model,
                gateway=ToolGateway(
                    registry=empty, policy=EnvelopePolicyEngine(registry=empty)
                ),
                policy_identity="triage-eval",
                model_label=arguments.model_id,
            ),
            timeout_seconds=arguments.timeout_seconds,
            sink_for=lambda stream_id: ScopedEventSink(
                log=InMemoryEventLog(),
                scope=EventScope(stream_id=stream_id, run_id=stream_id),
            ),
        )
        principal = PrincipalContext(tenant_id="eval", principal_id="triage-eval")

        rows: list[dict[str, Any]] = []
        for case in cases:
            result = await service.triage(principal, objective=case.objective)
            observed = _observed(result)
            rows.append(
                {
                    "objective": case.objective,
                    "expected": case.expected,
                    "observed": observed,
                    "reason": result.reason,
                    "question": result.question,
                    "correct": observed == case.expected,
                }
            )
            mark = "✓" if observed == case.expected else "✗"
            print(f"{mark} [{case.expected:>8} -> {observed:>8}] {case.objective[:48]}")

    by_class: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = by_class.setdefault(row["expected"], {"total": 0, "correct": 0})
        bucket["total"] += 1
        bucket["correct"] += int(row["correct"])
    total = len(rows)
    correct = sum(int(row["correct"]) for row in rows)

    report = {
        "gold_digest": digest,
        "model_id": arguments.model_id,
        "case_count": total,
        "accuracy": round(correct / total, 4) if total else None,
        "by_class": {
            label: {
                **counts,
                "accuracy": round(counts["correct"] / counts["total"], 4),
            }
            for label, counts in sorted(by_class.items())
        },
        "defaults": sum(1 for row in rows if row["observed"] == "default"),
        "cases": rows,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\naccuracy {correct}/{total}; report -> {REPORT_PATH}")
    for label, counts in sorted(by_class.items()):
        print(f"  {label:>8}: {counts['correct']}/{counts['total']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
