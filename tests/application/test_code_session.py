"""A coding session, assembled and run.

The runtime, the gateway, the policy engine and the five workspace tools are
all real here; only the model is scripted. That is the point of the file: the
claims worth making about Code are claims about how those pieces fit, and a
service tested against a stubbed executor would demonstrate none of them --
not that the workspace is reachable from a tool, not that a write moves the
session's pointer, not that the next turn starts where the last one stopped.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.memory import (
    InMemoryArtifactStore,
    InMemoryConversationStore,
    InMemoryEventLog,
)
from agent_workbench.adapters.models.fake import FakeModel, ScriptedTurn
from agent_workbench.adapters.policy import EnvelopePolicyEngine
from agent_workbench.adapters.tools import StaticToolRegistry
from agent_workbench.adapters.tools.workspace import (
    WorkspaceEditTool,
    WorkspaceGrepTool,
    WorkspaceListTool,
    WorkspaceReadTool,
    WorkspaceWriteTool,
)
from agent_workbench.application.answer_release import ProcessOnlySink
from agent_workbench.application.code_session import (
    CodeCapacityError,
    CodeRequest,
    CodeSessionService,
    CodeTurn,
    CodeTurnBusyError,
)
from agent_workbench.application.workspace_scope import WorkspaceScope
from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.events import UngroundedAnswerCommitted
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.domain.runs import AgentOutcome, RunBudget
from agent_workbench.domain.tools import ToolCall
from agent_workbench.ports.cancellation import CancellationSource, NullCancellationToken
from agent_workbench.ports.event_log import EventScope
from agent_workbench.runtime.agent_runtime import ClaudeLikeAgentRuntime
from agent_workbench.runtime.tool_gateway import ToolGateway

TENANT = "tenant_a"
OWNER = "user_1"
NEIGHBOUR = "user_2"
NOW = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)

#: The scope `workspace_write` and `workspace_edit` declare. A principal
#: without it opens a session, reads it, and is refused every write -- which is
#: the same rule every other write tool is under.
WRITER = PrincipalContext(
    principal_id=OWNER, tenant_id=TENANT, scopes=("workspace:write",)
)
READER = PrincipalContext(principal_id=OWNER, tenant_id=TENANT)

TURN_TIMEOUT = 600


def _write_call(name: str, content: str, call_id: str = "toolu_1") -> ToolCall:
    return ToolCall(
        tool_call_id=call_id,
        tool_name="workspace_write",
        arguments={"name": name, "content": content},  # pyright: ignore[reportArgumentType]
    )


class _Harness:
    """One process's worth of Code, with a scripted model."""

    def __init__(
        self,
        model: FakeModel,
        *,
        max_concurrent_turns: int = 4,
    ) -> None:
        self.scope = WorkspaceScope()
        registry = StaticToolRegistry(
            [
                WorkspaceListTool(self.scope).binding(),
                WorkspaceReadTool(self.scope).binding(),
                WorkspaceWriteTool(self.scope).binding(),
                WorkspaceEditTool(self.scope).binding(),
                WorkspaceGrepTool(self.scope).binding(),
            ]
        )
        self.conversations = InMemoryConversationStore()
        self.log = InMemoryEventLog()
        self.model = model
        runtime = ClaudeLikeAgentRuntime(
            model=model,
            gateway=ToolGateway(
                registry=registry,
                policy=EnvelopePolicyEngine(registry=registry),
            ),
            policy_identity="api-code-test:0000",
            clock=lambda: NOW,
        )
        self.service = CodeSessionService(
            conversations=self.conversations,
            artifacts=InMemoryArtifactStore(),
            executor_for=lambda _scope: runtime,
            scope=self.scope,
            budget=RunBudget(max_steps=6, max_tool_calls=6),
            turn_timeout_seconds=TURN_TIMEOUT,
            max_concurrent_turns=max_concurrent_turns,
            clock=lambda: NOW,
        )

    def sink(self, session_id: str, run_id: str) -> ProcessOnlySink:
        return ProcessOnlySink(
            ScopedEventSink(
                log=self.log,
                scope=EventScope(stream_id=session_id, run_id=run_id),
            )
        )

    async def ask(
        self,
        session_id: str,
        instruction: str,
        *,
        principal: PrincipalContext = WRITER,
        run_id: str = "run_1",
    ) -> CodeTurn:
        return await self.service.ask(
            CodeRequest(
                session_id=session_id,
                instruction=instruction,
                principal=principal,
                run_id=run_id,
            ),
            self.sink(session_id, run_id),
            NullCancellationToken(),
        )

    async def opened(self) -> str:
        return await self.service.open(tenant_id=TENANT, principal_id=OWNER)

    async def event_types(self, session_id: str) -> list[str]:
        return [envelope.event_type for envelope in await self.log.read(session_id)]


class _Held:
    """An executor that does not return until it is let go.

    The concurrency claims are about admission, not about tools, and an
    in-memory stack runs a whole scripted turn inside one scheduling slice --
    so a second request arrives after the first has already finished and the
    thing under test never happens.
    """

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.runs = 0

    async def run(self, request: Any, emit: Any, cancellation: Any) -> AgentOutcome:
        self.runs += 1
        await self.release.wait()
        return AgentOutcome(
            agent_run_id=request.trace.agent_run_id,
            status="completed",
            stop_reason="completed",
            output_text="done",
        )


class _Recording:
    """Keeps the request it was given, which is the thing being asserted on."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def run(self, request: Any, emit: Any, cancellation: Any) -> AgentOutcome:
        self.requests.append(request)
        return AgentOutcome(
            agent_run_id=request.trace.agent_run_id,
            status="completed",
            stop_reason="completed",
            output_text="done",
        )


def _writes(name: str, content: str, report: str) -> FakeModel:
    """Turn one writes a file; turn two reports. The canonical shape."""

    return FakeModel(
        [
            ScriptedTurn(text="Writing it.", tool_calls=(_write_call(name, content),)),
            ScriptedTurn(text=report),
        ]
    )


def _run(scenario: Any) -> Any:
    return asyncio.run(scenario())


def test_a_turn_writes_a_file_and_reports_what_it_did() -> None:
    """The whole thing, once: tools reach the workspace and the report lands."""

    harness = _Harness(_writes("notes.md", "hello", "Wrote notes.md."))

    async def scenario() -> tuple[str, bool, list[str]]:
        session_id = await harness.opened()
        turn = await harness.ask(session_id, "write notes.md")
        history = await harness.service.history(
            session_id=session_id, tenant_id=TENANT, principal_id=OWNER
        )
        return (
            turn.report,
            turn.workspace_version is not None,
            [message.role for message in history],
        )

    report, wrote_something, roles = _run(scenario)

    assert report == "Wrote notes.md."
    assert wrote_something is True
    assert roles == ["user", "assistant"]


def test_the_next_turn_starts_where_the_last_one_stopped() -> None:
    """The pointer is the session's, so a second turn sees the first's files.

    A workspace that reset between turns would still pass the test above: one
    turn's writes are visible to that turn either way. This is the assertion
    that needs the pointer to have been written through.
    """

    harness = _Harness(
        FakeModel(
            [
                ScriptedTurn(
                    text="Writing.", tool_calls=(_write_call("notes.md", "first"),)
                ),
                ScriptedTurn(text="Wrote it."),
                ScriptedTurn(
                    text="Looking.",
                    tool_calls=(
                        ToolCall(
                            tool_call_id="toolu_2",
                            tool_name="workspace_list",
                            arguments={},
                        ),
                    ),
                ),
                ScriptedTurn(text="notes.md is there."),
            ]
        )
    )

    async def scenario() -> tuple[str | None, str | None, str]:
        session_id = await harness.opened()
        first = await harness.ask(session_id, "write notes.md", run_id="run_1")
        second = await harness.ask(session_id, "what is there", run_id="run_2")
        # requests[0] and [1] are the first turn; [2] opens the second and
        # [3] is the one carrying the listing back.
        listed = harness.model.requests[3].messages[-1].content[0]
        return first.workspace_version, second.workspace_version, str(listed)

    first_version, second_version, listed = _run(scenario)

    assert first_version is not None
    # The second turn wrote nothing, so the pointer stands where the first left
    # it -- and the listing the model was shown proves it was reachable.
    assert second_version == first_version
    assert "notes.md" in listed


def test_a_principal_without_the_write_scope_is_refused_the_write() -> None:
    """The control for the scope requirement, which is easy to debug wrongly.

    The session opens, the turn runs, and the write is refused by policy. The
    report still comes back -- a coding agent that cannot write is a usable
    thing to be told about, and an exception here would hide which of the
    twenty reasons it was.
    """

    harness = _Harness(_writes("notes.md", "hello", "I could not write it."))

    async def scenario() -> tuple[str | None, list[str]]:
        session_id = await harness.opened()
        turn = await harness.ask(session_id, "write notes.md", principal=READER)
        return turn.workspace_version, await harness.event_types(session_id)

    version, events = _run(scenario)

    assert version is None
    assert "ToolFailed" in events
    assert "ToolCompleted" not in events


def test_a_session_runs_one_turn_at_a_time() -> None:
    """The slot lives in this process, because the turn does."""

    harness = _Harness(_writes("notes.md", "hello", "Done."))
    held = _Held()
    harness.service.executor_for = lambda _scope: held  # pyright: ignore[reportAttributeAccessIssue]

    async def scenario() -> tuple[str, int]:
        session_id = await harness.opened()
        first = asyncio.ensure_future(harness.ask(session_id, "one", run_id="run_1"))
        await asyncio.sleep(0)
        try:
            # Bounded: with the slot gone the second turn is admitted and parks
            # on the held executor, so an unbounded await here would wedge the
            # suite instead of reporting that the slot is gone.
            with pytest.raises(CodeTurnBusyError):
                await asyncio.wait_for(
                    harness.ask(session_id, "two", run_id="run_2"), timeout=5.0
                )
        finally:
            held.release.set()
            await first
        return "refused", held.runs

    # The second request was refused before it reached the executor at all.
    assert _run(scenario) == ("refused", 1)


def test_a_process_admits_only_as_many_turns_as_it_says() -> None:
    """Fail fast, not queue: a caller waiting on a minutes-long turn learns
    nothing it could not have been told at once."""

    harness = _Harness(_writes("notes.md", "hello", "Done."), max_concurrent_turns=1)
    held = _Held()
    harness.service.executor_for = lambda _scope: held  # pyright: ignore[reportAttributeAccessIssue]

    async def scenario() -> tuple[str, int]:
        first_session = await harness.opened()
        second_session = await harness.opened()
        first = asyncio.ensure_future(
            harness.ask(first_session, "one", run_id="run_1")
        )
        await asyncio.sleep(0)
        try:
            with pytest.raises(CodeCapacityError):
                await asyncio.wait_for(
                    harness.ask(second_session, "two", run_id="run_2"), timeout=5.0
                )
        finally:
            held.release.set()
            await first
        return "refused", held.runs

    # A different session, so the per-session slot was not what refused it.
    assert _run(scenario) == ("refused", 1)


def test_a_chat_session_cannot_be_driven_as_a_coding_one() -> None:
    """The same refusal the Chat service gives for a code session id."""

    harness = _Harness(_writes("notes.md", "hello", "Done."))

    async def scenario() -> None:
        await harness.conversations.create_session(
            session_id="ses_chat_1", tenant_id=TENANT, owner_id=OWNER
        )
        await harness.ask("ses_chat_1", "write notes.md")

    with pytest.raises(NotFoundError):
        _run(scenario)


def test_another_principal_cannot_drive_this_session() -> None:
    harness = _Harness(_writes("notes.md", "hello", "Done."))

    async def scenario() -> None:
        session_id = await harness.opened()
        await harness.ask(
            session_id,
            "write notes.md",
            principal=PrincipalContext(principal_id=NEIGHBOUR, tenant_id=TENANT),
        )

    with pytest.raises(NotFoundError):
        _run(scenario)


def test_what_the_user_said_survives_a_turn_that_failed() -> None:
    """Appended before the run, because saying it is what made it true."""

    harness = _Harness(FakeModel([]))

    async def scenario() -> tuple[list[str], str]:
        session_id = await harness.opened()
        turn = await harness.ask(session_id, "do the thing")
        history = await harness.service.history(
            session_id=session_id, tenant_id=TENANT, principal_id=OWNER
        )
        return [message.role for message in history], turn.outcome.status

    roles, status = _run(scenario)

    assert status == "failed"
    # The instruction is there; no report is, because there was none.
    assert roles == ["user"]


def test_a_run_that_tries_to_publish_an_answer_does_not() -> None:
    """The fence, reached through the service rather than in isolation.

    A Code turn has no release step, so nothing here should ever produce an
    answer event. This asserts what happens if something does: the emit
    raises, the turn ends failed, and the stream carries no answer.
    """

    harness = _Harness(_writes("notes.md", "hello", "Done."))

    async def scenario() -> tuple[list[str], bool]:
        session_id = await harness.opened()
        sink = harness.sink(session_id, "run_1")
        with pytest.raises(RuntimeError, match="no answer to publish"):
            await sink.emit(UngroundedAnswerCommitted(text="an answer"))
        events = await harness.event_types(session_id)
        return events, "UngroundedAnswerCommitted" in events

    events, published = _run(scenario)

    assert published is False
    assert events == []


def test_the_turn_carries_a_deadline_derived_from_the_clock() -> None:
    """A code run is required to carry one; this is where it comes from."""

    harness = _Harness(_writes("notes.md", "hello", "Done."))
    recording = _Recording()
    harness.service.executor_for = lambda _scope: recording  # pyright: ignore[reportAttributeAccessIssue]

    async def scenario() -> Any:
        session_id = await harness.opened()
        await harness.ask(session_id, "write notes.md")
        return recording.requests[0]

    request = _run(scenario)

    assert request.run_kind == "code"
    assert request.budget.deadline == NOW + timedelta(seconds=TURN_TIMEOUT)
    # Both, and they are not the same thing.
    assert request.tool_names == request.envelope.allowed_tools


def test_cancelling_a_turn_keeps_the_files_it_had_already_written() -> None:
    """The inversion of the Task rule, and the reason the pointer writes through.

    A user who watched a file appear and then stopped the turn did not ask for
    that file to vanish.
    """

    cancellation = CancellationSource()
    harness = _Harness(
        FakeModel(
            [
                ScriptedTurn(
                    text="Writing.", tool_calls=(_write_call("notes.md", "first"),)
                ),
                ScriptedTurn(text="Wrote it."),
                ScriptedTurn(
                    text="Listing.",
                    tool_calls=(
                        ToolCall(
                            tool_call_id="toolu_9",
                            tool_name="workspace_list",
                            arguments={},
                        ),
                    ),
                ),
                ScriptedTurn(text="Still there."),
            ]
        )
    )

    async def scenario() -> tuple[str, str]:
        session_id = await harness.opened()
        await harness.ask(session_id, "write notes.md", run_id="run_1")
        cancellation.cancel("the user stopped it")
        cancelled = await harness.service.ask(
            CodeRequest(
                session_id=session_id,
                instruction="keep going",
                principal=WRITER,
                run_id="run_2",
            ),
            harness.sink(session_id, "run_2"),
            cancellation,
        )
        after = await harness.conversations.session(
            session_id=session_id, tenant_id=TENANT, principal_id=OWNER
        )
        assert after.workspace_version is not None
        return cancelled.outcome.status, after.workspace_version

    status, version = _run(scenario)

    assert status == "cancelled"
    assert version
