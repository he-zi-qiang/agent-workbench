"""The agentic shape, and the fence it is not allowed to skip.

The fixed shape retrieves once, so what an answer rests on is whatever that one
retrieval authorized. The agentic shape searches inside the model loop, which
means the same question has a different answer to "what was this built from" --
and getting that answer wrong is how a revoked document reaches a reader.

So the properties here are about the journal and the envelope:

* every search a run performs is fenced, not only the ones the model cited;
* a run leaves nothing behind, however it ended, so evidence cannot be
  attributed to whichever run asks next;
* the tool is granted by permission rather than by instruction, and the fixed
  shape grants nothing at all.

No database: what is under test is the shape, and the release fence's own
transaction has its own suite against real PostgreSQL.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_workbench.application.chat_execution import (
    AGENTIC_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    AgenticExecution,
    ChatRequest,
    FixedTwoStepExecution,
    RetrievalJournal,
    TurnExecution,
    build_agentic_request,
    build_fixed_request,
    merge_authorized,
    merge_citations,
)
from agent_workbench.application.retrieval import AuthorizedContext
from agent_workbench.domain.context import Citation, ContextChunk, ContextPacket
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.domain.runs import AgentOutcome, AgentRunRequest, RunBudget
from agent_workbench.ports.cancellation import NullCancellationToken

PRINCIPAL = PrincipalContext(tenant_id="tenant_a", principal_id="user_1")


def _request(**overrides: Any) -> ChatRequest:
    base: dict[str, Any] = {
        "session_id": "ses_1",
        "question": "How does the index handle deletions?",
        "principal": PRINCIPAL,
        "knowledge_base_id": "kb_main",
        "idempotency_key": "key_1",
        "run_id": "run_1",
    }
    base.update(overrides)
    return ChatRequest(**base)


def _context(document_id: str, revision: int, chunk: str) -> AuthorizedContext:
    return AuthorizedContext(
        packet=ContextPacket(
            chunks=(
                ContextChunk(
                    chunk_id=chunk,
                    document_id=document_id,
                    document_version=f"v{revision}",
                    tenant_id="tenant_a",
                    text="Deletions are tombstoned and reconciled.",
                ),
            ),
            citations=(
                Citation(
                    document_id=document_id,
                    chunk_id=chunk,
                    document_version=f"v{revision}",
                ),
            ),
        ),
        authorized_revisions=((document_id, revision),),
    )


@dataclass
class _Executor:
    """A model loop that searches whatever it was told to, then answers."""

    journal: RetrievalJournal
    searches: tuple[AuthorizedContext, ...] = ()
    requests: list[AgentRunRequest] = field(default_factory=list)
    fail: bool = False

    async def run(
        self, request: AgentRunRequest, sink: object, cancellation: object
    ) -> AgentOutcome:
        self.requests.append(request)
        for context in self.searches:
            # What the tool does, without the gateway in the way.
            self.journal.record(request.trace.agent_run_id, context)
        if self.fail:
            raise RuntimeError("the provider died mid-loop")
        return AgentOutcome(
            agent_run_id=request.trace.agent_run_id,
            status="completed",
            stop_reason="completed",
            output_text="Deletions are tombstoned.",
        )


def _agentic(executor: _Executor, journal: RetrievalJournal) -> AgenticExecution:
    return AgenticExecution(
        executor=executor,  # type: ignore[arg-type]
        journal=journal,
        budget=RunBudget(max_steps=4, max_tool_calls=6),
        tool_names=("knowledge_search",),
    )


async def _produce(execution: AgenticExecution, request: ChatRequest) -> Any:
    return await execution.produce(
        request,
        history=(),
        sink=object(),  # type: ignore[arg-type]
        cancellation=NullCancellationToken(),
    )


# --------------------------------------------------------------------------
# The fence
# --------------------------------------------------------------------------


def test_every_search_a_run_performs_is_fenced_not_only_the_last() -> None:
    """Three searches, three documents, and all three re-checked.

    Keeping only the final packet is the natural mistake -- it is what the
    fixed shape's single retrieval looks like -- and it would leave an answer
    built on the first two searches unfenced.
    """

    journal = RetrievalJournal()
    executor = _Executor(
        journal=journal,
        searches=(
            _context("doc_1", 3, "chunk_a"),
            _context("doc_2", 1, "chunk_b"),
            _context("doc_3", 7, "chunk_c"),
        ),
    )

    produced = asyncio.run(_produce(_agentic(executor, journal), _request()))

    assert produced.authorized_revisions == (("doc_1", 3), ("doc_2", 1), ("doc_3", 7))
    # The fake answer names none of them, so it earns no citations -- the fence
    # is wider than the sources on purpose. Which ids an answer earns is tested
    # in tests/application/test_citations.py.
    assert produced.citations == ()


def test_a_document_seen_at_two_revisions_is_checked_at_both() -> None:
    """The answer may have drawn on either, and the fence cannot tell which."""

    journal = RetrievalJournal()
    executor = _Executor(
        journal=journal,
        searches=(
            _context("doc_1", 3, "chunk_a"),
            _context("doc_1", 4, "chunk_a2"),
            # And the same revision twice is still one entry.
            _context("doc_1", 3, "chunk_a"),
        ),
    )

    produced = asyncio.run(_produce(_agentic(executor, journal), _request()))

    assert produced.authorized_revisions == (("doc_1", 3), ("doc_1", 4))


def test_a_run_that_searched_nothing_carries_no_evidence() -> None:
    """The control group: the fence is empty because the run really is."""

    journal = RetrievalJournal()
    produced = asyncio.run(
        _produce(_agentic(_Executor(journal=journal), journal), _request())
    )

    assert produced.authorized_revisions == ()
    assert produced.citations == ()


# --------------------------------------------------------------------------
# The journal's lifetime
# --------------------------------------------------------------------------


def test_a_finished_run_leaves_nothing_in_the_journal() -> None:
    journal = RetrievalJournal()
    executor = _Executor(journal=journal, searches=(_context("doc_1", 1, "chunk_a"),))

    asyncio.run(_produce(_agentic(executor, journal), _request()))

    assert journal.pending_runs() == 0


def test_a_failed_run_leaves_nothing_either() -> None:
    """The case a ``finally`` exists for.

    A run that raised still searched. Its entries would otherwise sit in the
    journal until some later run happened to share its id -- or, worse, be
    counted as evidence for a different question.
    """

    journal = RetrievalJournal()
    executor = _Executor(
        journal=journal, searches=(_context("doc_1", 1, "chunk_a"),), fail=True
    )

    with pytest.raises(RuntimeError, match="died mid-loop"):
        asyncio.run(_produce(_agentic(executor, journal), _request()))

    assert journal.pending_runs() == 0


def test_two_concurrent_runs_do_not_read_each_others_evidence() -> None:
    """One tool binding serves the whole process, so the key has to be the run."""

    journal = RetrievalJournal()

    async def scenario() -> tuple[Any, Any]:
        first = _agentic(
            _Executor(journal=journal, searches=(_context("doc_1", 1, "chunk_a"),)),
            journal,
        )
        second = _agentic(
            _Executor(journal=journal, searches=(_context("doc_2", 2, "chunk_b"),)),
            journal,
        )
        return await asyncio.gather(
            _produce(first, _request(run_id="run_1")),
            _produce(second, _request(run_id="run_2")),
        )

    one, two = asyncio.run(scenario())

    assert one.authorized_revisions == (("doc_1", 1),)
    assert two.authorized_revisions == (("doc_2", 2),)
    assert journal.pending_runs() == 0


# --------------------------------------------------------------------------
# Authority
# --------------------------------------------------------------------------


def test_the_agentic_envelope_grants_the_search_tool_and_nothing_else() -> None:
    request = build_agentic_request(
        _request(),
        RunBudget(max_steps=4, max_tool_calls=6),
        tool_names=("knowledge_search",),
    )

    assert request.envelope.allowed_tools == ("knowledge_search",)
    assert request.tool_names == ("knowledge_search",)
    # The ceiling stays where it was. Searching is a read, so nothing had to be
    # raised to permit it -- and a write tool still could not be reached.
    assert request.envelope.max_tool_risk == "read"
    assert request.system_prompt == AGENTIC_SYSTEM_PROMPT
    # No packet: there is nothing retrieved yet, and a prompt carrying evidence
    # would be the fixed shape wearing the agentic one's name.
    assert request.context is None


def test_the_fixed_envelope_still_grants_nothing() -> None:
    """The control group, and the property the fixed shape exists to have."""

    request = build_fixed_request(
        _request(),
        ContextPacket(),
        RunBudget(max_steps=1, max_tool_calls=1),
    )

    assert request.envelope.allowed_tools == ()
    assert request.tool_names == ()
    assert request.system_prompt == SYSTEM_PROMPT


def test_an_agentic_run_with_no_tool_is_refused_rather_than_run() -> None:
    """It would ask the model to search and give it no way to.

    A run like that does not fail loudly -- it produces an answer from nothing
    with an empty fence behind it, which is the worst of both shapes.
    """

    with pytest.raises(ValueError, match="granted a retrieval tool"):
        build_agentic_request(
            _request(), RunBudget(max_steps=4, max_tool_calls=6), tool_names=()
        )


def test_both_shapes_satisfy_the_seam() -> None:
    journal = RetrievalJournal()
    assert isinstance(_agentic(_Executor(journal=journal), journal), TurnExecution)
    assert isinstance(
        FixedTwoStepExecution(
            retrieval=object(),  # type: ignore[arg-type]
            executor=object(),  # type: ignore[arg-type]
            budget=RunBudget(max_steps=1, max_tool_calls=1),
        ),
        TurnExecution,
    )


# --------------------------------------------------------------------------
# The merges, on their own
# --------------------------------------------------------------------------


def test_merging_is_order_independent_and_deduplicated() -> None:
    a = _context("doc_2", 1, "chunk_b")
    b = _context("doc_1", 3, "chunk_a")

    assert merge_authorized((a, b)) == merge_authorized((b, a))
    assert merge_authorized((a, b, a)) == (("doc_1", 3), ("doc_2", 1))
    assert len(merge_citations((a, b, a))) == 2


# --------------------------------------------------------------------------
# The tool that actually writes the journal
# --------------------------------------------------------------------------


def test_the_search_tool_journals_what_it_authorized_under_its_own_run() -> None:
    """The production writer, which the executor fake above stands in for.

    Everything above proves the execution reads the journal correctly. This is
    the other half: that anything writes it, and writes it under the run that
    searched. A sabotage round found both missing -- the fake was journalling on
    the tool's behalf, so the tool could have stopped and nothing would have
    noticed.
    """

    from agent_workbench.adapters.tools.knowledge_search import KnowledgeSearchTool
    from agent_workbench.domain.policies import AuthorizationEnvelope, ExecutionContext
    from agent_workbench.domain.tools import ToolCall
    from agent_workbench.ports.tools import ToolInvocation

    recorded = _context("doc_1", 2, "chunk_a")

    class _Retrieval:
        async def retrieve(self, request: Any) -> AuthorizedContext:
            return recorded

    journal = RetrievalJournal()
    tool = KnowledgeSearchTool(
        retrieval=_Retrieval(),  # type: ignore[arg-type]
        journal=journal,
    )
    invocation = ToolInvocation(
        call=ToolCall(
            tool_call_id="toolu_01",
            tool_name="knowledge_search",
            arguments={"query": "deletions", "knowledge_base_id": "kb_main"},
        ),
        context=ExecutionContext(
            principal=PRINCIPAL,
            envelope=AuthorizationEnvelope(),
            agent_run_id="run_7",
            policy_identity="p:f",
        ),
        cancellation=NullCancellationToken(),
        timeout_seconds=30,
    )

    result = asyncio.run(tool.handle(invocation))

    assert result.status == "ok"
    # Under run_7, and only under run_7.
    assert journal.take("run_7") == (recorded,)
    assert journal.take("run_other") == ()


def test_a_tool_without_a_journal_still_answers() -> None:
    """The control group: journalling is what the chat turn needs, not the tool."""

    from agent_workbench.adapters.tools.knowledge_search import KnowledgeSearchTool
    from agent_workbench.domain.policies import AuthorizationEnvelope, ExecutionContext
    from agent_workbench.domain.tools import ToolCall
    from agent_workbench.ports.tools import ToolInvocation

    class _Retrieval:
        async def retrieve(self, request: Any) -> AuthorizedContext:
            return _context("doc_1", 2, "chunk_a")

    tool = KnowledgeSearchTool(retrieval=_Retrieval())  # type: ignore[arg-type]
    result = asyncio.run(
        tool.handle(
            ToolInvocation(
                call=ToolCall(
                    tool_call_id="toolu_01",
                    tool_name="knowledge_search",
                    arguments={"query": "deletions", "knowledge_base_id": "kb_main"},
                ),
                context=ExecutionContext(
                    principal=PRINCIPAL,
                    envelope=AuthorizationEnvelope(),
                    agent_run_id="run_7",
                    policy_identity="p:f",
                ),
                cancellation=NullCancellationToken(),
                timeout_seconds=30,
            )
        )
    )

    assert result.status == "ok"


def test_an_agentic_budget_that_cannot_be_built_is_refused_at_startup() -> None:
    """A run may spend a tool call on every step.

    Caught in settings because the alternative is a process that starts, serves
    fixed turns happily, and fails only once somebody switches the shape.
    """

    from pydantic import ValidationError as PydanticValidationError

    from agent_workbench.bootstrap.settings import ChatSettings

    with pytest.raises(PydanticValidationError, match="max_agentic_searches"):
        ChatSettings(max_agentic_steps=8, max_agentic_searches=4)

    # The control group: the same pair the other way round builds.
    assert ChatSettings(max_agentic_steps=4, max_agentic_searches=8) is not None
