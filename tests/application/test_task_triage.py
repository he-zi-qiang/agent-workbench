"""Triage decides, asks, or defaults -- and never blocks a submission.

Every test drives the real service against the fake executor; what varies is
what the "model" says. The default-outcome tests each pair with a decided
control so a service that answered "default" to everything could not pass.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.memory.event_log import InMemoryEventLog
from agent_workbench.application.task_triage import (
    FALLBACK_QUESTION,
    TaskTriageService,
    TriageResult,
)
from agent_workbench.domain.messages import TextBlock
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.domain.runs import AgentOutcome, AgentRunRequest
from agent_workbench.ports.event_log import EventScope
from agent_workbench.runtime.fake_executor import FakeAgentExecutor

PRINCIPAL = PrincipalContext(tenant_id="tenant_a", principal_id="user_1")


def _service(
    executor: FakeAgentExecutor, *, timeout_seconds: float = 5.0
) -> TaskTriageService:
    return TaskTriageService(
        executor=executor,
        timeout_seconds=timeout_seconds,
        sink_for=lambda stream_id: ScopedEventSink(
            log=InMemoryEventLog(),
            scope=EventScope(stream_id=stream_id, run_id=stream_id),
        ),
    )


def _verdict(**fields: object) -> str:
    return json.dumps(fields, ensure_ascii=False)


async def _triage(
    service: TaskTriageService, objective: str = "把三个 CSV 合并"
) -> TriageResult:
    return await service.triage(PRINCIPAL, objective=objective)


def test_a_clear_verdict_is_decided() -> None:
    executor = FakeAgentExecutor(
        respond=lambda request: _verdict(
            graph="general", wants_report=False, reason="要做一件事", question=None
        )
    )
    result = asyncio.run(_triage(_service(executor)))

    assert result == TriageResult(
        status="decided", graph="general", wants_report=False, reason="要做一件事"
    )
    assert len(executor.requests) == 1


def test_the_classifier_declines_to_think() -> None:
    """The fifth ``run_kind="chat"`` builder, pinned like the other four.

    Nothing shows a classifier's reasoning, the caller holds a ten-second
    deadline over this call, and the output budget is sized for one small JSON
    object -- reasoning would spend both on text no reader sees, and a
    truncated verdict falls back to the default silently (ADR-061).
    """

    executor = FakeAgentExecutor(
        respond=lambda request: _verdict(
            graph="general", wants_report=False, reason="要做一件事", question=None
        )
    )
    asyncio.run(_triage(_service(executor)))

    assert executor.requests[0].thinking is False


def test_an_unsure_graph_becomes_a_question_not_a_guess() -> None:
    executor = FakeAgentExecutor(
        respond=lambda request: _verdict(
            graph="unsure",
            wants_report=False,
            reason="两种都通",
            question="是要调研报告，还是直接把事做完？",
        )
    )
    result = asyncio.run(_triage(_service(executor)))

    assert result.status == "ask"
    assert result.question == "是要调研报告，还是直接把事做完？"
    assert result.graph is None


def test_an_unsure_graph_with_no_question_gets_the_fallback_wording() -> None:
    executor = FakeAgentExecutor(
        respond=lambda request: _verdict(
            graph="unsure", wants_report=False, reason="含糊", question=None
        )
    )
    result = asyncio.run(_triage(_service(executor)))

    assert result.status == "ask"
    assert result.question == FALLBACK_QUESTION


def test_an_unsure_wants_report_resolves_to_false_without_asking() -> None:
    """The control group is the test below: only the wants_report differs."""

    executor = FakeAgentExecutor(
        respond=lambda request: _verdict(
            graph="research", wants_report="unsure", reason="像调研", question=None
        )
    )
    result = asyncio.run(_triage(_service(executor)))

    assert result == TriageResult(
        status="decided", graph="research", wants_report=False, reason="像调研"
    )


def test_a_true_wants_report_survives() -> None:
    executor = FakeAgentExecutor(
        respond=lambda request: _verdict(
            graph="research", wants_report=True, reason="要文件", question=None
        )
    )
    result = asyncio.run(_triage(_service(executor)))

    assert result.wants_report is True


def test_a_framing_failure_earns_exactly_one_corrective_turn() -> None:
    answers = iter(
        [
            '好的，这就给你分类：{"graph": "general"}',
            _verdict(
                graph="general", wants_report=False, reason="第二次", question=None
            ),
        ]
    )
    executor = FakeAgentExecutor(respond=lambda request: next(answers))
    result = asyncio.run(_triage(_service(executor)))

    assert result.status == "decided"
    assert result.reason == "第二次"
    assert len(executor.requests) == 2
    # The corrective turn replays the unreadable answer as the model's own.
    replayed = executor.requests[1].messages[-2]
    assert replayed.role == "assistant"
    assert any(
        isinstance(block, TextBlock) and "好的" in block.text
        for block in replayed.content
    )


def test_two_framing_failures_default() -> None:
    executor = FakeAgentExecutor(respond=lambda request: "not json, ever")
    result = asyncio.run(_triage(_service(executor)))

    assert result == TriageResult(status="default")
    assert len(executor.requests) == 2


def test_a_wrong_claim_defaults_without_a_second_turn() -> None:
    """A parseable-but-invalid answer is not re-asked (ADR-034's boundary).

    The framing test above is the control: same service, two runs there, one
    run here.
    """

    executor = FakeAgentExecutor(
        respond=lambda request: _verdict(
            graph="banana", wants_report=False, reason="", question=None
        )
    )
    result = asyncio.run(_triage(_service(executor)))

    assert result == TriageResult(status="default")
    assert len(executor.requests) == 1


def test_a_timeout_defaults_instead_of_blocking_the_form() -> None:
    class _Hanging:
        def __init__(self) -> None:
            self.calls = 0

        async def run(
            self, request: AgentRunRequest, emit, cancellation
        ) -> AgentOutcome:
            self.calls += 1
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

    hanging = _Hanging()
    service = TaskTriageService(
        executor=hanging,
        timeout_seconds=0.05,
        sink_for=lambda stream_id: ScopedEventSink(
            log=InMemoryEventLog(),
            scope=EventScope(stream_id=stream_id, run_id=stream_id),
        ),
    )
    result = asyncio.run(_triage(service))

    assert result == TriageResult(status="default")
    assert hanging.calls == 1


def test_a_raising_executor_defaults_instead_of_propagating() -> None:
    class _Raising:
        async def run(self, request, emit, cancellation):
            raise RuntimeError("provider exploded")

    result = asyncio.run(
        TaskTriageService(
            executor=_Raising(),
            timeout_seconds=1.0,
            sink_for=lambda stream_id: ScopedEventSink(
                log=InMemoryEventLog(),
                scope=EventScope(stream_id=stream_id, run_id=stream_id),
            ),
        ).triage(PRINCIPAL, objective="anything")
    )

    assert result == TriageResult(status="default")


def test_the_prompt_carries_the_form_facts() -> None:
    executor = FakeAgentExecutor(
        respond=lambda request: _verdict(
            graph="general", wants_report=False, reason="ok", question=None
        )
    )
    asyncio.run(
        _service(executor).triage(
            PRINCIPAL,
            objective="整理这批合同",
            knowledge_base_selected=True,
            attachment_names=("a.pdf", "b.md"),
        )
    )

    prompt = " ".join(
        block.text
        for block in executor.requests[0].messages[-1].content
        if isinstance(block, TextBlock)
    )
    assert "整理这批合同" in prompt
    assert "knowledge base is attached" in prompt
    assert "a.pdf" in prompt
    # And the run is toolless and deny-shaped by construction.
    assert executor.requests[0].tool_names == ()


@pytest.mark.parametrize("objective", ["研究一下这批反馈"])
def test_the_service_is_usable_concurrently(objective: str) -> None:
    """Two overlapping calls share nothing mutable but the executor's log."""

    executor = FakeAgentExecutor(
        respond=lambda request: _verdict(
            graph="research", wants_report=False, reason="r", question=None
        )
    )
    service = _service(executor)

    async def _both() -> tuple[TriageResult, TriageResult]:
        return await asyncio.gather(
            service.triage(PRINCIPAL, objective=objective),
            service.triage(PRINCIPAL, objective=objective),
        )

    first, second = asyncio.run(_both())
    assert first.status == second.status == "decided"
    # Each call minted its own stream: the two requests must not share one.
    streams = {request.stream_id for request in executor.requests}
    assert len(streams) == 2
