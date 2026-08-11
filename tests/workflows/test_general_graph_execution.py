"""The compiled v2 graph, driven through the same adapter as v1.

``test_general_graph.py`` pins the routing as pure functions. This file is
about what only a compiled graph can show: that the adapter's v2 assembly
reproduces that declaration -- the back edge actually re-runs the work node,
the revision counter advances exactly when a verdict is being answered, the
worker reads the complaint it is fixing, and the one human interrupt pauses a
v2 thread the same way it pauses a v1 thread.

The handlers are deterministic stand-ins.  The real work/review handlers have
their own contract tests in ``test_task_handlers.py``; here they would only
obscure which graph edge a failure came from.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from agent_workbench.adapters.langgraph import build_approval_node
from agent_workbench.adapters.langgraph.workflow import LangGraphTaskWorkflow
from agent_workbench.domain.policies import AuthorizationEnvelope
from agent_workbench.domain.tasks import ReviewResult, TaskState
from agent_workbench.ports.approvals import ApprovalRecord
from agent_workbench.ports.task_registry import TaskRun
from agent_workbench.ports.task_workflow import ApprovalResume
from agent_workbench.workflows.approval import TaskApprovalGate

NOW = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)

V2 = "v2_general"


def _state(**overrides: object) -> TaskState:
    base: dict[str, object] = {
        "task_id": "task_1",
        "objective": "Fix the failing script until it runs.",
        "max_revisions": 1,
    }
    base.update(overrides)
    return TaskState.model_validate(base)


def _review(decision: str, draft_ref: str, revision: int) -> dict[str, Any]:
    return ReviewResult(
        decision=decision,
        reviewed_draft_ref=draft_ref,
        revision_number=revision,
        summary="Checked the working set against the objective.",
        issues=() if decision == "pass" else ("The script still exits non-zero.",),
        score=90 if decision == "pass" else 35,
    ).model_dump()


class _Script:
    """Deterministic work/review handlers that record what each pass saw."""

    def __init__(self, verdicts: list[str]) -> None:
        #: What the reviewer will say, in order. The last entry repeats.
        self.verdicts = verdicts
        self.work_saw: list[tuple[int, str | None]] = []
        self.reviews_given = 0

    def handlers(self) -> dict[str, Any]:
        async def understand(_: TaskState) -> dict[str, Any]:
            return {}

        async def work(state: TaskState) -> dict[str, Any]:
            # Record the state as the *handler* received it: on a revision
            # pass the verdict must still be present, because the complaint is
            # why this node is running again -- and the counter has not moved,
            # because a state carrying both an advanced counter and the old
            # verdict is one ``TaskState`` refuses to validate. The bounded
            # transition lands in the checkpoint, not in this view.
            self.work_saw.append(
                (
                    state.revision_count,
                    None
                    if state.review_result is None
                    else state.review_result.decision,
                )
            )
            attempt = len(self.work_saw) - 1
            return {
                # Named after the attempt, not the revision the state shows:
                # the real node's artifact store mints a fresh id per run, and
                # a reviewer binding to a stale draft is what this catches.
                "draft_ref": f"draft_attempt_{attempt}",
                "review_result": None,
            }

        async def review(state: TaskState) -> dict[str, Any]:
            assert state.draft_ref is not None
            verdict = self.verdicts[min(self.reviews_given, len(self.verdicts) - 1)]
            self.reviews_given += 1
            return {
                "review_result": _review(verdict, state.draft_ref, state.revision_count)
            }

        async def approval(state: TaskState) -> dict[str, Any]:
            return {"approval_id": "apr_demo", "approval_decision": "approved"}

        async def export(_: TaskState) -> dict[str, Any]:
            return {"export_ref": "art_export_1"}

        return {
            "understand": understand,
            "work": work,
            "review": review,
            "approval": approval,
            "export": export,
        }


def _run(workflow: LangGraphTaskWorkflow, state: TaskState, thread: str) -> Any:
    return asyncio.run(workflow.run(state, thread_id=thread, graph_version=V2))


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


def test_a_revise_verdict_sends_the_work_back_through_the_compiled_edge() -> None:
    script = _Script(["revise", "pass"])
    result = _run(
        LangGraphTaskWorkflow(handlers=script.handlers()), _state(), "thread_loop"
    )

    assert result.disposition == "completed"
    # Two passes: the first with nothing to answer, the second carrying the
    # reviewer's verdict -- the wrapper closes it only after the handler ran.
    assert script.work_saw == [(0, None), (0, "revise")]
    assert script.reviews_given == 2
    # The second pass's draft is the one the export path saw, and the bounded
    # transition reached the checkpoint even though the handler never saw it.
    assert result.state.draft_ref == "draft_attempt_1"
    assert result.state.revision_count == 1
    assert result.state.export_ref == "art_export_1"


def test_the_second_attempt_reads_the_complaint_it_is_answering() -> None:
    """The verdict reaches the work handler, unlike v1's writer.

    This is the ordering ``revision_update`` exists for, stated against the
    compiled graph: v1 clears the review *before* its writer runs, v2 after,
    because v2's second attempt without the complaint is a coin flip rather
    than a fix.
    """

    script = _Script(["revise", "pass"])
    _run(LangGraphTaskWorkflow(handlers=script.handlers()), _state(), "thread_read")

    revision_pass = script.work_saw[1]
    assert revision_pass == (0, "revise")


def test_an_exhausted_budget_fails_with_v2s_own_wording() -> None:
    script = _Script(["revise"])
    result = _run(
        LangGraphTaskWorkflow(handlers=script.handlers()),
        _state(max_revisions=1),
        "thread_spent",
    )

    assert result.disposition == "failed"
    assert result.failure_reason == (
        "review still requires changes after 1 revisions of the work node"
    )
    # The budget bounded the loop: first pass, one revision, no third attempt.
    assert script.work_saw == [(0, None), (0, "revise")]
    # And nothing was approved or exported on the way out.
    assert result.state.approval_id is None
    assert result.state.export_ref is None


def test_passing_work_nobody_asked_a_file_for_completes_without_the_gate() -> None:
    script = _Script(["pass"])
    result = _run(
        LangGraphTaskWorkflow(handlers=script.handlers()),
        _state(wants_report=False),
        "thread_nofile",
    )

    assert result.disposition == "completed"
    assert result.failure_reason is None
    assert result.state.approval_id is None
    assert result.state.export_ref is None


def test_an_ungated_deployment_exports_through_a_declared_edge() -> None:
    """Against the *compiled* graph, which is the only place this could fail.

    ``route_review`` gained "export" when the approval gate became optional,
    and the edge list beside it did not. Every routing test still passed --
    they call the router directly -- while LangGraph resolves the router's
    answer against that list, so the first real Task submitted after the
    change died inside the graph on `KeyError: 'export'`.

    Running the graph is what closes the gap: a target the router can return
    and no edge declares cannot survive this call.
    """

    script = _Script(["pass"])
    result = _run(
        LangGraphTaskWorkflow(handlers=script.handlers()),
        _state(export_requires_approval=False),
        "thread_ungated",
    )

    assert result.disposition == "completed"
    assert result.state.export_ref == "art_export_1"
    # Skipped, not auto-answered: nothing opened an approval and nothing
    # recorded a decision nobody made.
    assert result.state.approval_id is None
    assert result.state.approval_decision is None


def test_the_gated_deployment_still_stops_at_the_approval() -> None:
    """The control for the test above, on the same compiled graph.

    Without it, "the export edge exists" could be satisfied by a graph that
    had stopped honouring the gate at all.
    """

    script = _Script(["pass"])
    result = _run(
        LangGraphTaskWorkflow(handlers=script.handlers()),
        _state(export_requires_approval=True),
        "thread_gated",
    )

    assert result.disposition == "completed"
    assert result.state.approval_id == "apr_demo"
    assert result.state.export_ref == "art_export_1"


# --------------------------------------------------------------------------
# The human interrupt, on a v2 thread
# --------------------------------------------------------------------------


def _task_row() -> TaskRun:
    return TaskRun.model_validate(
        {
            "task_id": "task_1",
            "tenant_id": "tenant_a",
            "owner_id": "user_1",
            "thread_id": "thread_gate",
            "graph_version": V2,
            "input_ref": "input_1",
            "input_fingerprint": "a" * 64,
            "submission_dedup_key": "dedup_1",
            "run_semantics_snapshot": {},
            "run_semantics_revision": "semantics_1",
            "submitted_policy_revision": "policy_1",
            "submitted_policy_fingerprint": "b" * 16,
            "submitted_authorization_envelope": AuthorizationEnvelope(),
            "status": "running",
            "attempt_count": 1,
            "available_at": NOW,
            "created_at": NOW,
            "updated_at": NOW,
        }
    )


class _Registry:
    def __init__(self, task: TaskRun) -> None:
        self._task = task

    async def get(self, _: str) -> TaskRun | None:
        return self._task


class _Ledger:
    """The minimal approvals ledger the interrupting node's protocol needs."""

    def __init__(self) -> None:
        self.records: dict[str, ApprovalRecord] = {}

    async def request(
        self,
        *,
        task_id: str,
        graph_node_operation_id: str,
        tenant_id: str,
        owner_id: str,
    ) -> ApprovalRecord:
        for record in self.records.values():
            if (
                record.task_id == task_id
                and record.graph_node_operation_id == graph_node_operation_id
            ):
                return record
        record = ApprovalRecord(
            approval_id=f"apr_{len(self.records) + 1}",
            task_id=task_id,
            graph_node_operation_id=graph_node_operation_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
            status="pending",
            created_at=NOW,
        )
        self.records[record.approval_id] = record
        return record

    async def get(self, approval_id: str) -> ApprovalRecord | None:
        return self.records.get(approval_id)

    def decide(self, approval_id: str, decision: str) -> None:
        stored = self.records[approval_id]
        self.records[approval_id] = stored.model_copy(
            update={
                "status": decision,
                "decision_version": 1,
                "decided_by": "user_1",
                "decided_at": NOW,
            }
        )


def _interrupting_workflow() -> tuple[LangGraphTaskWorkflow, _Ledger, _Script]:
    script = _Script(["pass"])
    ledger = _Ledger()
    handlers = script.handlers()
    # The real interrupting node, exactly as the composition root wires it --
    # the same gate object serves both graphs, which is the ADR-031 §2.4 claim
    # ("same export approval") in executable form.
    handlers["approval"] = build_approval_node(
        TaskApprovalGate(
            approvals=ledger,  # type: ignore[arg-type]
            registry=_Registry(_task_row()),  # type: ignore[arg-type]
        )
    )
    return LangGraphTaskWorkflow(handlers=handlers), ledger, script


def test_a_v2_thread_pauses_at_the_approval_and_names_it() -> None:
    workflow, ledger, _ = _interrupting_workflow()

    async def scenario() -> Any:
        result = await workflow.run(_state(), thread_id="thread_gate", graph_version=V2)
        return result, await workflow.inspect("thread_gate")

    result, position = asyncio.run(scenario())

    assert result.disposition == "interrupted"
    assert result.next_nodes == ("approval",)
    assert position is not None
    assert position.graph_version == V2
    assert position.pending_nodes == ("approval",)
    assert position.awaiting_approval_id == "apr_1"
    assert ledger.records["apr_1"].status == "pending"
    # The node raised before returning: nothing about the approval is state.
    assert result.state.approval_id is None


@pytest.mark.parametrize(
    ("decision", "disposition", "export_ref"),
    [
        ("approved", "completed", "art_export_1"),
        ("rejected", "failed", None),
    ],
)
def test_the_decision_decides_the_v2_path(
    decision: str, disposition: str, export_ref: str | None
) -> None:
    workflow, ledger, _ = _interrupting_workflow()

    async def scenario() -> Any:
        await workflow.run(_state(), thread_id="thread_gate", graph_version=V2)
        ledger.decide("apr_1", decision)
        return await workflow.resume(
            thread_id="thread_gate",
            graph_version=V2,
            approval=ApprovalResume(approval_id="apr_1", decision_version=1),
        )

    result = asyncio.run(scenario())

    assert result.disposition == disposition
    assert result.state.approval_decision == decision
    assert result.state.export_ref == export_ref
    if decision == "rejected":
        assert result.failure_reason == "export was rejected by a reviewer"
