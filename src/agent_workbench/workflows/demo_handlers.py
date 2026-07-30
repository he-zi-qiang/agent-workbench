"""Deterministic, offline handlers for the fixed v1 Task graph.

This module is deliberately a demonstration fixture, not an Agent Runtime
adapter.  It neither contacts a provider nor executes a tool, and the
identifiers it puts in ``evidence_refs`` and ``draft_ref`` name *synthetic*
artifacts that do not exist in an ArtifactStore.  Its only job is to make the
real LangGraph control flow usable in a fast smoke test while PR-D's real
handler assembly is still separate work.

Keeping this factory out of bootstrap and settings is intentional: a deployed
worker must be assembled with real handlers explicitly.  A configuration flag
that could select these handlers would make a successful Task look like a
report-producing Agent when no report bytes were ever stored.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from agent_workbench.domain.tasks import ReviewResult, TaskNodeId, TaskState, TaskStep

DemoNodeHandler = Callable[[TaskState], Awaitable[Mapping[str, Any]]]


def build_demo_v1_handlers() -> dict[TaskNodeId, DemoNodeHandler]:
    """Return a complete, deterministic handler set for the v1 graph.

    The handlers deliberately exercise the graph's data invariants:

    * ``plan`` creates an ordered plan, enabling the fixed research fan-out;
    * both research branches write distinct references for the reducer to
      merge;
    * synthesis writes a revision-specific draft reference; and
    * critic binds a passing verdict to that exact draft and revision.

    No returned reference is an assertion that bytes exist.  Production code
    must use the PR-D handler factory, which writes and reads actual artifacts.
    """

    async def understand(state: TaskState) -> dict[str, Any]:
        return _outcome_update(state, "understand")

    async def plan(state: TaskState) -> dict[str, Any]:
        step = TaskStep(
            step_id=_stable_id("step", state.task_id, "plan", "1"),
            sequence=1,
            objective="Produce a concise, evidence-oriented answer to the objective.",
        )
        return _outcome_update(state, "plan") | {"plan": (step.model_dump(),)}

    async def research_internal(state: TaskState) -> dict[str, Any]:
        return _research_update(state, "research_internal")

    async def research_external(state: TaskState) -> dict[str, Any]:
        return _research_update(state, "research_external")

    async def synthesize(state: TaskState) -> dict[str, Any]:
        # The LangGraph adapter advances revision_count before a synthesize
        # retry.  Including it in the synthetic reference verifies that the
        # critic reviews the draft actually written by this pass.
        draft_ref = _stable_id(
            "art_demo",
            state.task_id,
            "draft",
            str(state.revision_count),
        )
        return _outcome_update(state, "synthesize") | {
            "draft_ref": draft_ref,
            "review_result": None,
        }

    async def critic(state: TaskState) -> dict[str, Any]:
        if state.draft_ref is None:
            raise ValueError("the demo critic requires a draft")
        review = ReviewResult(
            decision="pass",
            reviewed_draft_ref=state.draft_ref,
            revision_number=state.revision_count,
            summary="Deterministic demo review: the draft is accepted.",
            score=100,
        )
        return _outcome_update(state, "critic") | {
            "review_result": review.model_dump(),
        }

    async def approval(state: TaskState) -> dict[str, Any]:
        # This is not a real human interrupt.  It merely provides a legal
        # passing-state value so the fixed v1 graph reaches export in CI.
        return _outcome_update(state, "approval") | {
            "approval_id": _stable_id("apr_demo", state.task_id, "approval"),
        }

    async def export(state: TaskState) -> dict[str, Any]:
        return _outcome_update(state, "export")

    return {
        "understand": understand,
        "plan": plan,
        "research_internal": research_internal,
        "research_external": research_external,
        "synthesize": synthesize,
        "critic": critic,
        "approval": approval,
        "export": export,
    }


def _research_update(state: TaskState, node: str) -> dict[str, Any]:
    """Return this branch's independent contribution for LangGraph fan-in."""

    return _outcome_update(state, node) | {
        # Synthetic reference only; see the module docstring.
        "evidence_refs": (_stable_id("art_demo", state.task_id, node),),
    }


def _outcome_update(state: TaskState, node: str) -> dict[str, Any]:
    """Record a stable synthetic node-run reference without replacing peers."""

    return {
        "agent_outcome_refs": (
            _stable_id("run_demo", state.task_id, node, str(state.revision_count)),
        )
    }


def _stable_id(prefix: str, *parts: str) -> str:
    """Make an Identifier-safe, stable demo id without copying user text."""

    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:32]}"


__all__ = ["build_demo_v1_handlers"]
