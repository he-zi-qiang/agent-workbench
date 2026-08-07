"""Per-turn Direct/RAG selection without splitting the Chat lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_workbench.application.chat import _request_hash
from agent_workbench.application.chat_execution import (
    AnswerMode,
    AnswerModeSelector,
    ChatRequest,
    ProducedAnswer,
    TurnExecution,
)
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.domain.runs import AgentOutcome
from agent_workbench.ports.cancellation import NullCancellationToken

PRINCIPAL = PrincipalContext(tenant_id="tenant_a", principal_id="user_1")


def _request(
    answer_mode: AnswerMode = "rag",
    knowledge_base_id: str | None = "kb_main",
    *,
    key: str = "key_1",
) -> ChatRequest:
    return ChatRequest(
        session_id="ses_same",
        question="How does this work?",
        principal=PRINCIPAL,
        knowledge_base_id=knowledge_base_id,
        idempotency_key=key,
        answer_mode=answer_mode,
        run_id=f"run_{key}",
    )


@dataclass
class _Execution:
    grounded: bool
    calls: list[ChatRequest] = field(default_factory=list)

    async def produce(
        self,
        request: ChatRequest,
        *,
        history: tuple[Any, ...],
        sink: Any,
        cancellation: Any,
    ) -> ProducedAnswer:
        del history, sink, cancellation
        self.calls.append(request)
        return ProducedAnswer(
            outcome=AgentOutcome(
                agent_run_id=request.run_id,
                status="completed",
                stop_reason="completed",
                output_text="direct" if not self.grounded else "rag",
            ),
            grounded=self.grounded,
            authorized_revisions=() if not self.grounded else (("doc_1", 1),),
            citations=(),
        )


async def _produce(
    selector: AnswerModeSelector, request: ChatRequest
) -> ProducedAnswer:
    return await selector.produce(
        request,
        history=(),
        sink=object(),  # pyright: ignore[reportArgumentType]
        cancellation=NullCancellationToken(),
    )


def test_application_callers_default_to_the_historical_rag_mode() -> None:
    assert _request().answer_mode == "rag"


def test_one_session_can_mix_direct_and_rag_turns() -> None:
    """Mode belongs to the turn; choosing again must not fork the session."""

    direct = _Execution(grounded=False)
    rag = _Execution(grounded=True)
    selector = AnswerModeSelector(
        direct=direct,  # pyright: ignore[reportArgumentType]
        rag=rag,  # pyright: ignore[reportArgumentType]
    )

    direct_result, rag_result = asyncio.run(_mixed_turns(selector))

    assert direct_result.grounded is False
    assert rag_result.grounded is True
    assert [request.session_id for request in (*direct.calls, *rag.calls)] == [
        "ses_same",
        "ses_same",
    ]
    assert [request.answer_mode for request in (*direct.calls, *rag.calls)] == [
        "direct",
        "rag",
    ]


async def _mixed_turns(
    selector: AnswerModeSelector,
) -> tuple[ProducedAnswer, ProducedAnswer]:
    direct = await _produce(selector, _request("direct", None, key="direct"))
    rag = await _produce(selector, _request("rag", "kb_main", key="rag"))
    return direct, rag


@pytest.mark.parametrize(
    ("chat_request", "message"),
    [
        (_request("direct", "kb_main"), "must not name"),
        (_request("rag", None), "requires a knowledge base"),
    ],
)
def test_selector_rejects_incoherent_application_requests(
    chat_request: ChatRequest, message: str
) -> None:
    selector = AnswerModeSelector(
        direct=_Execution(False),  # pyright: ignore[reportArgumentType]
        rag=_Execution(True),  # pyright: ignore[reportArgumentType]
    )

    with pytest.raises(ValueError, match=message):
        asyncio.run(_produce(selector, chat_request))


def test_direct_only_deployment_rejects_rag_below_http_too() -> None:
    selector = AnswerModeSelector(
        direct=_Execution(False),  # pyright: ignore[reportArgumentType]
        rag=None,
    )

    with pytest.raises(ValueError, match="unavailable"):
        asyncio.run(_produce(selector, _request()))


def test_answer_mode_is_part_of_the_idempotency_request_hash() -> None:
    """A mode change is a semantic change even under one client key."""

    direct_shape = _request("direct", None)
    # Keep every other field, including the absent knowledge base, identical so
    # only ``answer_mode`` can account for the different digest. This second
    # request is intentionally not executed; the selector would correctly
    # reject a RAG request without a knowledge base.
    rag_shape = _request("rag", None)

    assert _request_hash(direct_shape) != _request_hash(rag_shape)


def test_selector_satisfies_the_execution_seam() -> None:
    selector = AnswerModeSelector(
        direct=_Execution(False),  # pyright: ignore[reportArgumentType]
        rag=_Execution(True),  # pyright: ignore[reportArgumentType]
    )

    assert isinstance(selector, TurnExecution)
