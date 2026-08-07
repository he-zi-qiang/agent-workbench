"""Routing between a grounded answer and an ungrounded one.

The switch is made on authorized evidence, so the tests drive it by varying
what retrieval returns and assert on three things that must move together: the
prompt the model was handed, the ``grounded`` flag on the result, and the
citations. A shape that flipped one without the others would either publish an
unverified answer as verified, or ask a model with no evidence to cite some.

The empty-retrieval case is the one that matters most, and it is not a corner:
it is what every question asked against a knowledge base that does not cover it
looks like. Before this shape existed, that produced a refusal.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_workbench.application.chat_execution import (
    SYSTEM_PROMPT,
    UNGROUNDED_SYSTEM_PROMPT,
    ChatRequest,
    RoutedExecution,
)
from agent_workbench.application.retrieval import AuthorizedContext
from agent_workbench.domain.context import Citation, ContextChunk, ContextPacket
from agent_workbench.domain.events import RetrievalRejected
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.domain.runs import (
    AgentOutcome,
    BudgetUsage,
    RunBudget,
    TokenUsage,
)
from agent_workbench.ports.conversation_store import AuthorizedRevision, ChatTurnResult

TENANT = "tenant_a"
PRINCIPAL = "user_reader"
KB = "kb_main"

#: Real chunk id shapes. The citation pattern requires 8-64 hex digits after
#: the prefix, so a friendly placeholder like "chk_a" is not recognised as a
#: citation at all -- a fixture that used one would report every citation as
#: fabricated and look like a broken verifier.
CHUNK_A = "chk_57934adabf3bac65ec44ccc6f67d8c87"
CHUNK_GHOST = "chk_da5366c13ab9b4d4a2b0ac4d9d9d8e39"


def _packet(*chunk_ids: str) -> ContextPacket:
    chunks = tuple(
        ContextChunk(
            chunk_id=chunk_id,
            document_id=f"doc_{chunk_id}",
            document_version="ver_1",
            tenant_id=TENANT,
            text=f"passage {chunk_id}",
            score=1.0,
        )
        for chunk_id in chunk_ids
    )
    # Citations alongside the chunks, because that is what RetrievalService
    # builds: verify_citations checks the answer against `packet.citations`, so
    # a fixture with chunks and no citations would report every real citation
    # as fabricated -- a fixture bug that reads exactly like a product bug.
    citations = tuple(
        Citation(
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            document_version=chunk.document_version,
        )
        for chunk in chunks
    )
    return ContextPacket(chunks=chunks, citations=citations)


class _Retrieval:
    """Returns whatever context the test wants, and counts the asking."""

    def __init__(self, context: AuthorizedContext) -> None:
        self._context = context
        self.calls = 0

    async def retrieve(self, request: Any) -> AuthorizedContext:
        self.calls += 1
        return self._context


class _Executor:
    """Records the request it was handed, and answers with fixed text."""

    def __init__(self, text: str = "an answer") -> None:
        self._text = text
        self.requests: list[Any] = []

    async def run(self, request: Any, sink: Any, cancellation: Any) -> AgentOutcome:
        self.requests.append(request)
        return AgentOutcome(
            agent_run_id=request.trace.agent_run_id,
            status="completed",
            stop_reason="completed",
            output_text=self._text,
            usage=BudgetUsage(
                steps=1,
                tool_calls=0,
                tokens=TokenUsage(input_tokens=4, output_tokens=4),
            ),
        )


class _Sink:
    """Collects what routing emits.

    This used to refuse every emission ("routing must not emit events of its
    own"), which held while the fallback was silent. It is no longer true, and
    the reason is ADR-018's own justification for this shape: it is allowed
    *because* the decision leaves a trace. `UngroundedAnswerCommitted` records
    that the answer was unverified; it does not record that retrieval ran and
    was rejected, so a turn that searched and fell back looked identical to one
    that never searched.
    """

    def __init__(self) -> None:
        self.emitted: list[Any] = []

    async def emit(self, payload: Any, **kwargs: Any) -> Any:
        self.emitted.append(payload)


def _run(
    *,
    context: AuthorizedContext,
    answer: str = "an answer",
    threshold: float = 0.0,
) -> tuple[Any, _Retrieval, _Executor, _Sink]:
    retrieval = _Retrieval(context)
    executor = _Executor(answer)
    sink = _Sink()
    execution = RoutedExecution(
        retrieval=retrieval,  # pyright: ignore[reportArgumentType]
        executor=executor,  # pyright: ignore[reportArgumentType]
        budget=RunBudget(max_steps=1, max_tool_calls=1),
        relevance_threshold=threshold,
    )
    produced = asyncio.run(
        execution.produce(
            ChatRequest(
                session_id="ses_1",
                question="where does fusion happen",
                principal=PrincipalContext(tenant_id=TENANT, principal_id=PRINCIPAL),
                knowledge_base_id=KB,
                idempotency_key="key-1",
            ),
            history=(),
            sink=sink,  # pyright: ignore[reportArgumentType]
            cancellation=None,  # pyright: ignore[reportArgumentType]
        )
    )
    return produced, retrieval, executor, sink


# --- evidence found: the grounded path, unchanged ---------------------------


def test_evidence_routes_to_the_grounded_answer() -> None:
    produced, retrieval, executor, _sink = _run(
        context=AuthorizedContext(
            packet=_packet(CHUNK_A),
            authorized_revisions=((f"doc_{CHUNK_A}", 1),),
            top_relevance=5.0,
        ),
        answer=f"fusion happens in Qdrant [{CHUNK_A}]",
    )

    assert retrieval.calls == 1
    assert produced.grounded is True
    assert produced.authorized_revisions == ((f"doc_{CHUNK_A}", 1),)
    assert executor.requests[0].system_prompt == SYSTEM_PROMPT


def test_a_grounded_route_still_verifies_citations() -> None:
    """Routing must not become a way around the citation check."""

    produced, _, _, _sink = _run(
        context=AuthorizedContext(
            packet=_packet(CHUNK_A),
            authorized_revisions=((f"doc_{CHUNK_A}", 1),),
            top_relevance=5.0,
        ),
        answer=f"see [{CHUNK_A}] and also [{CHUNK_GHOST}]",
    )

    assert tuple(c.chunk_id for c in produced.citations) == (CHUNK_A,)
    assert produced.fabricated_citations == (CHUNK_GHOST,)


# --- nothing found: the ungrounded fallback ---------------------------------


def test_no_evidence_routes_to_the_ungrounded_answer() -> None:
    produced, retrieval, executor, _sink = _run(
        context=AuthorizedContext(packet=ContextPacket(), authorized_revisions=())
    )

    # It still asked. Routing on the question instead would let a knowledge
    # question be answered from memory while the corpus held the answer.
    assert retrieval.calls == 1
    assert produced.grounded is False
    assert executor.requests[0].system_prompt == UNGROUNDED_SYSTEM_PROMPT


def test_the_ungrounded_route_carries_no_evidence_claims() -> None:
    produced, _, _, _sink = _run(
        context=AuthorizedContext(packet=ContextPacket(), authorized_revisions=())
    )

    assert produced.citations == ()
    assert produced.authorized_revisions == ()
    assert produced.fabricated_citations == ()


def test_the_ungrounded_route_does_not_ask_for_citations() -> None:
    """The fixed prompt would tell a model with no evidence to cite chunk ids.

    It would also tell it to say that no evidence was retrieved rather than
    answer -- which is the refusal this shape exists to replace.
    """

    _, _, executor, _sink = _run(
        context=AuthorizedContext(packet=ContextPacket(), authorized_revisions=())
    )
    prompt = executor.requests[0]

    assert "Cite the chunk ids" not in prompt.system_prompt
    assert "No evidence was retrieved" not in prompt.messages[-1].content


def test_an_unreadable_corpus_falls_back_rather_than_refusing() -> None:
    """Evidence filtered out by the ACL check looks like no evidence.

    Deliberately: refusing would tell the asker that a document they may not
    read exists. The fallback answer discloses nothing the corpus did not
    already imply.
    """

    produced, _, executor, _sink = _run(
        context=AuthorizedContext(packet=ContextPacket(), authorized_revisions=())
    )

    assert produced.grounded is False
    assert executor.requests[0].system_prompt == UNGROUNDED_SYSTEM_PROMPT
    # And the trace it leaves says nothing the asker could not already infer:
    # zero surviving chunks, exactly as an empty corpus reports.
    rejected = _only_rejection(_sink)
    assert rejected.chunk_count == 0


# --- the fallback leaves a trace --------------------------------------------


def _only_rejection(sink: _Sink) -> RetrievalRejected:
    rejections = [one for one in sink.emitted if isinstance(one, RetrievalRejected)]
    assert len(rejections) == 1
    return rejections[0]


def test_the_fallback_records_that_retrieval_ran_and_was_rejected() -> None:
    """Without this, an ungrounded turn cannot be told from one that never
    searched -- and ADR-018 allows this shape *because* the decision is traced.
    """

    context = AuthorizedContext(
        packet=_packet(CHUNK_A),
        authorized_revisions=(("doc_1", 1),),
        top_relevance=0.25,
    )

    _, _, _, sink = _run(context=context, threshold=0.5)
    rejected = _only_rejection(sink)

    assert rejected.chunk_count == 1
    assert rejected.top_relevance == 0.25
    # The threshold travels with the score: it is configuration, and an
    # operator deciding whether to lower it needs both numbers together.
    assert rejected.threshold == 0.5


def test_a_grounded_turn_records_no_rejection() -> None:
    context = AuthorizedContext(
        packet=_packet(CHUNK_A),
        authorized_revisions=(("doc_1", 1),),
        top_relevance=0.9,
    )

    produced, _, _, sink = _run(context=context, threshold=0.5)

    assert produced.grounded is True
    assert [one for one in sink.emitted if isinstance(one, RetrievalRejected)] == []


def test_an_unmeasured_relevance_is_recorded_as_unmeasured() -> None:
    """`None` is a different reason from a low score, and stays distinguishable."""

    context = AuthorizedContext(
        packet=_packet(CHUNK_A),
        authorized_revisions=(("doc_1", 1),),
        top_relevance=None,
    )

    _, _, _, sink = _run(context=context, threshold=0.5)

    assert _only_rejection(sink).top_relevance is None


# --- what the stored result will accept -------------------------------------


def _result(*, grounded: bool, citations: tuple[Any, ...] = ()) -> ChatTurnResult:
    outcome = AgentOutcome(
        agent_run_id="run_1",
        status="completed",
        stop_reason="completed",
        output_text="an answer",
        usage=BudgetUsage(
            steps=1, tool_calls=0, tokens=TokenUsage(input_tokens=4, output_tokens=4)
        ),
    )
    return ChatTurnResult(
        outcome=outcome,
        answer="an answer",
        authorized_revisions=(),
        citations=citations,
        grounded=grounded,
    )


def test_an_ungrounded_result_may_not_carry_citations() -> None:
    """The invariant the release coordinator relies on, enforced at the type."""

    with pytest.raises(ValueError, match="ungrounded result"):
        _result(
            grounded=False,
            citations=(
                Citation(
                    chunk_id="chk_a", document_id="doc_a", document_version="ver_1"
                ),
            ),
        )


def test_an_ungrounded_result_may_not_carry_authorized_revisions() -> None:
    """Revisions would send the release fence to re-check unread documents."""

    outcome = AgentOutcome(
        agent_run_id="run_1",
        status="completed",
        stop_reason="completed",
        output_text="an answer",
        usage=BudgetUsage(
            steps=1, tool_calls=0, tokens=TokenUsage(input_tokens=4, output_tokens=4)
        ),
    )

    with pytest.raises(ValueError, match="ungrounded result"):
        ChatTurnResult(
            outcome=outcome,
            answer="an answer",
            authorized_revisions=(
                AuthorizedRevision(document_id="doc_a", source_revision=1),
            ),
            grounded=False,
        )


def test_a_stored_result_defaults_to_grounded() -> None:
    """Every row written before this field existed came from a retrieval path.

    Defaulting to False would relabel the whole history as unverified, which is
    the more damaging direction to be wrong in.
    """

    assert _result(grounded=True).grounded is True
    assert (
        ChatTurnResult.model_validate(
            _result(grounded=True).model_dump(exclude={"grounded"})
        ).grounded
        is True
    )


# --- the relevance gate -----------------------------------------------------


def test_irrelevant_evidence_routes_to_the_ungrounded_answer() -> None:
    """The case the first version of this shape got wrong.

    A vector search returns its k nearest neighbours whether or not any of them
    relate to the question, so "chunks came back" is almost always true. Here
    chunks came back and the cross-encoder scored them below the bar.
    """

    produced, _, executor, _sink = _run(
        context=AuthorizedContext(
            packet=_packet(CHUNK_A),
            authorized_revisions=((f"doc_{CHUNK_A}", 1),),
            top_relevance=-4.0,
        ),
        threshold=0.0,
    )

    assert produced.grounded is False
    assert produced.citations == ()
    assert executor.requests[0].system_prompt == UNGROUNDED_SYSTEM_PROMPT


def test_relevant_evidence_above_the_threshold_stays_grounded() -> None:
    """The control. Without it a gate that rejected everything would pass."""

    produced, _, executor, _sink = _run(
        context=AuthorizedContext(
            packet=_packet(CHUNK_A),
            authorized_revisions=((f"doc_{CHUNK_A}", 1),),
            top_relevance=0.5,
        ),
        threshold=0.0,
    )

    assert produced.grounded is True
    assert executor.requests[0].system_prompt == SYSTEM_PROMPT


def test_the_threshold_is_the_thing_that_decides() -> None:
    """Same evidence, same score, opposite outcomes from the threshold alone.

    This is what makes the previous two tests about the gate rather than about
    the fixture: nothing varies here except the configured bar.
    """

    context = AuthorizedContext(
        packet=_packet(CHUNK_A),
        authorized_revisions=((f"doc_{CHUNK_A}", 1),),
        top_relevance=1.0,
    )

    below, _, _, _sink = _run(context=context, threshold=0.5)
    above, _, _, _sink = _run(context=context, threshold=2.0)

    assert below.grounded is True
    assert above.grounded is False


def test_an_unmeasured_relevance_is_not_treated_as_relevant() -> None:
    """A failed-open reranker must not silently license the grounded path.

    ``None`` means nothing measured relevance. Reading it as "good enough"
    would answer from evidence whose relevance was never established, which is
    the exact failure this gate exists to prevent.
    """

    produced, _, executor, _sink = _run(
        context=AuthorizedContext(
            packet=_packet(CHUNK_A),
            authorized_revisions=((f"doc_{CHUNK_A}", 1),),
            top_relevance=None,
        ),
        threshold=0.0,
    )

    assert produced.grounded is False
    assert executor.requests[0].system_prompt == UNGROUNDED_SYSTEM_PROMPT
