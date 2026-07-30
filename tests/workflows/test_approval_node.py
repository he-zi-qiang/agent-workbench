"""The v1 graph's one human interrupt, and what wakes it.

Three properties are under test, and only the first is about LangGraph:

* the graph stops at the approval and the position says which approval it is
  stopped on -- with no state written, because the node raised before returning;
* resuming re-reads the decision from the ledger, so the resume payload cannot
  decide anything;
* the answer decides the path: approved exports, rejected does not.

The ledger here is a fake rather than PostgreSQL. What is under test is the
node's protocol with a ledger, and the real store's transaction has its own
suite against a real database.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from agent_workbench.adapters.langgraph import build_approval_node
from agent_workbench.adapters.langgraph.workflow import LangGraphTaskWorkflow
from agent_workbench.domain.policies import AuthorizationEnvelope
from agent_workbench.domain.tasks import ReviewResult, TaskState, TaskStep
from agent_workbench.ports.approvals import ApprovalRecord
from agent_workbench.ports.task_registry import TaskRun
from agent_workbench.ports.task_workflow import ApprovalResume
from agent_workbench.workflows.approval import (
    APPROVAL_OPERATION_ID,
    ApprovalNotDecidedError,
    ApprovalTaskUnavailableError,
    TaskApprovalGate,
)

NOW = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)


def _task(task_id: str = "task_1") -> TaskRun:
    return TaskRun.model_validate(
        {
            "task_id": task_id,
            "tenant_id": "tenant_a",
            "owner_id": "user_1",
            "thread_id": "thread_1",
            "graph_version": "v1",
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
    def __init__(self, task: TaskRun | None) -> None:
        self._task = task

    async def get(self, _: str) -> TaskRun | None:
        return self._task


@dataclass
class _Ledger:
    """An approvals ledger with the two properties the node relies on."""

    records: dict[str, ApprovalRecord] = field(default_factory=dict)
    requests: list[tuple[str, str]] = field(default_factory=list)

    async def request(
        self,
        *,
        task_id: str,
        graph_node_operation_id: str,
        tenant_id: str,
        owner_id: str,
    ) -> ApprovalRecord:
        self.requests.append((task_id, graph_node_operation_id))
        existing = next(
            (
                record
                for record in self.records.values()
                if record.task_id == task_id
                and record.graph_node_operation_id == graph_node_operation_id
            ),
            None,
        )
        if existing is not None:
            return existing
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

    async def decide(
        self,
        approval_id: str,
        *,
        decision: str,
        decision_version: int,
        decided_by: str,
    ) -> ApprovalRecord:
        stored = self.records[approval_id]
        decided = stored.model_copy(
            update={
                "status": decision,
                "decision_version": decision_version,
                "decided_by": decided_by,
                "decided_at": NOW,
            }
        )
        self.records[approval_id] = decided
        return decided


def _state() -> TaskState:
    return TaskState.model_validate(
        {
            "task_id": "task_1",
            "objective": "Compare retrieval strategies.",
            "plan": (
                TaskStep(
                    step_id="step_1", sequence=1, objective="Gather internal notes."
                ),
            ),
        }
    )


def _handlers(ledger: _Ledger, registry: _Registry) -> dict[str, Any]:
    """The deterministic graph, with the real interrupting approval node."""

    async def synthesize(_: TaskState) -> dict[str, Any]:
        return {"draft_ref": "draft_1", "review_result": None}

    async def critic(state: TaskState) -> dict[str, Any]:
        return {
            "review_result": ReviewResult(
                decision="pass",
                reviewed_draft_ref="draft_1",
                revision_number=state.revision_count,
                summary="Grounded in the evidence.",
                score=90,
            ).model_dump()
        }

    return {
        "synthesize": synthesize,
        "critic": critic,
        "approval": build_approval_node(
            TaskApprovalGate(approvals=ledger, registry=registry)  # type: ignore[arg-type]
        ),
    }


def _paused() -> tuple[LangGraphTaskWorkflow, _Ledger, Any]:
    """Run the graph until it stops at the approval, and report where."""

    ledger = _Ledger()
    workflow = LangGraphTaskWorkflow(handlers=_handlers(ledger, _Registry(_task())))

    async def scenario() -> Any:
        result = await workflow.run(_state(), thread_id="thread_1", graph_version="v1")
        position = await workflow.inspect("thread_1")
        return result, position

    result, position = asyncio.run(scenario())
    return workflow, ledger, (result, position)


# --------------------------------------------------------------------------
# Stopping
# --------------------------------------------------------------------------


def test_the_graph_stops_at_the_approval_and_names_the_one_it_waits_on() -> None:
    _, ledger, (result, position) = _paused()

    assert result.disposition == "interrupted"
    assert result.next_nodes == ("approval",)
    assert position is not None
    assert position.pending_nodes == ("approval",)
    assert position.awaiting_approval_id == "apr_1"
    # The node raised before returning, so nothing about the approval reached
    # the checkpointed state. The interrupt is the only place the id exists.
    assert result.state.approval_id is None
    assert result.state.approval_decision is None
    assert ledger.records["apr_1"].status == "pending"


def test_the_pause_opens_exactly_one_approval_for_the_graph_operation() -> None:
    _, ledger, _ = _paused()

    assert ledger.requests == [("task_1", APPROVAL_OPERATION_ID)]
    assert len(ledger.records) == 1


def test_an_ordinary_unfinished_position_awaits_no_approval() -> None:
    """The control group for the position above.

    Without it, ``awaiting_approval_id`` could be reporting the same value for
    every paused thread and the assertion above would not notice.
    """

    ledger = _Ledger()

    async def blocked_critic(_: TaskState) -> dict[str, Any]:
        raise AssertionError("the graph must stop before the critic runs")

    workflow = LangGraphTaskWorkflow(
        handlers=_handlers(ledger, _Registry(_task())) | {"critic": blocked_critic}
    )

    async def scenario() -> Any:
        with pytest.raises(AssertionError):
            await workflow.run(_state(), thread_id="thread_1", graph_version="v1")
        return await workflow.inspect("thread_1")

    position = asyncio.run(scenario())

    assert position is not None
    assert position.pending_nodes  # unfinished, but not at an approval
    assert position.awaiting_approval_id is None


# --------------------------------------------------------------------------
# Waking
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("decision", "expected_disposition"),
    [("approved", "completed"), ("rejected", "failed")],
)
def test_resuming_takes_the_path_the_recorded_decision_names(
    decision: str, expected_disposition: str
) -> None:
    exported: list[str] = []
    ledger = _Ledger()

    async def export(_: TaskState) -> dict[str, Any]:
        exported.append("export")
        return {}

    workflow = LangGraphTaskWorkflow(
        handlers=_handlers(ledger, _Registry(_task())) | {"export": export}
    )

    async def scenario() -> Any:
        await workflow.run(_state(), thread_id="thread_1", graph_version="v1")
        await ledger.decide(
            "apr_1", decision=decision, decision_version=1, decided_by="reviewer_1"
        )
        return await workflow.resume(
            thread_id="thread_1",
            graph_version="v1",
            approval=ApprovalResume(approval_id="apr_1", decision_version=1),
        )

    result = asyncio.run(scenario())

    assert result.disposition == expected_disposition
    assert result.state.approval_decision == decision
    assert result.state.approval_id == "apr_1"
    assert exported == (["export"] if decision == "approved" else [])
    assert (result.failure_reason is not None) is (decision == "rejected")


def test_the_resume_payload_cannot_decide_the_approval() -> None:
    """The ledger is the authority; the payload is only a wake-up.

    A resume naming a decision the ledger never recorded must not approve
    anything, and must not be read as a rejection either -- both would be
    verdicts nobody gave. The node fails closed instead.
    """

    ledger = _Ledger()
    workflow = LangGraphTaskWorkflow(handlers=_handlers(ledger, _Registry(_task())))

    async def scenario() -> None:
        await workflow.run(_state(), thread_id="thread_1", graph_version="v1")
        # Nothing was decided. The wake-up arrives anyway.
        with pytest.raises(ApprovalNotDecidedError):
            await workflow.resume(
                thread_id="thread_1",
                graph_version="v1",
                approval=ApprovalResume(approval_id="apr_1", decision_version=1),
            )

    asyncio.run(scenario())

    assert ledger.records["apr_1"].status == "pending"


def test_a_forged_approval_id_in_the_resume_is_not_the_one_read_back() -> None:
    """The node reads back the approval *it* opened, not the one it was handed.

    The control group is the test above: with the real approval decided, the
    same resume succeeds. Here a second, decided approval belonging to another
    Task is named in the payload, and it changes nothing.
    """

    ledger = _Ledger()
    workflow = LangGraphTaskWorkflow(handlers=_handlers(ledger, _Registry(_task())))

    async def scenario() -> None:
        await workflow.run(_state(), thread_id="thread_1", graph_version="v1")
        forged = await ledger.request(
            task_id="task_other",
            graph_node_operation_id=APPROVAL_OPERATION_ID,
            tenant_id="tenant_a",
            owner_id="user_1",
        )
        await ledger.decide(
            forged.approval_id,
            decision="approved",
            decision_version=1,
            decided_by="attacker",
        )
        with pytest.raises(ApprovalNotDecidedError) as captured:
            await workflow.resume(
                thread_id="thread_1",
                graph_version="v1",
                approval=ApprovalResume(
                    approval_id=forged.approval_id, decision_version=1
                ),
            )
        assert captured.value.approval_id == "apr_1"

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# The gate on its own
# --------------------------------------------------------------------------


def test_the_gate_refuses_to_open_an_approval_for_a_task_it_cannot_find() -> None:
    gate = TaskApprovalGate(approvals=_Ledger(), registry=_Registry(None))  # type: ignore[arg-type]

    with pytest.raises(ApprovalTaskUnavailableError):
        asyncio.run(gate.open(_state()))


def test_the_gate_attributes_an_approval_to_the_registry_row_not_the_state() -> None:
    ledger = _Ledger()
    gate = TaskApprovalGate(approvals=ledger, registry=_Registry(_task()))  # type: ignore[arg-type]

    record = asyncio.run(gate.open(_state()))

    assert (record.tenant_id, record.owner_id) == ("tenant_a", "user_1")
