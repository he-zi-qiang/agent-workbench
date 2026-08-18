"""Driving a coding session over HTTP.

The service is tested against a real runtime elsewhere. What is left here is
what only the route layer can be wrong about: which requests are refused and
with which status, that a code session id and a chat session id are not
interchangeable in either direction, and that the router is absent from a
process that does not run coding turns.

The executor is a stub for exactly that reason. A scripted model would make
these tests slower and would put the thing under test -- the status codes --
behind a tool loop that has nothing to do with them.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from agent_workbench.adapters.memory import (
    InMemoryArtifactStore,
    InMemoryConversationStore,
    InMemoryEventLog,
)
from agent_workbench.adapters.tools.sandbox import WorkspaceSandbox
from agent_workbench.application.code_approvals import (
    ApprovalNotPendingError,
    ApprovalScope,
    CodeApprovalRegistry,
    StandingApprovalRefusedError,
)
from agent_workbench.application.code_session import (
    CodeCapacityError,
    CodeRunNotPermittedError,
    CodeRunRefusedError,
    CodeRunUnavailableError,
    CodeSessionService,
    CodeTurnBusyError,
)
from agent_workbench.application.workspace_scope import WorkspaceScope
from agent_workbench.apps.api.dependencies import build_dependencies
from agent_workbench.apps.api.main import ERROR_STATUS, create_app
from agent_workbench.apps.api.routes import code as code_route
from agent_workbench.apps.api.sse import LiveEventChannel
from agent_workbench.apps.api.state import STATE_ATTRIBUTE
from agent_workbench.bootstrap import Settings
from agent_workbench.bootstrap.paths import DEFAULT_CONFIG_FILE
from agent_workbench.bootstrap.projections import project_api
from agent_workbench.domain.errors import NotFoundError, ToolInputInvalidError
from agent_workbench.domain.events import UngroundedAnswerCommitted
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.domain.runs import AgentOutcome, RunBudget
from agent_workbench.ports.event_log import EventScope

#: The module whose warning these two tests are about.
_API_LOGGER = "agent_workbench.apps.api.main"


@contextmanager
def _warnings_from(name: str) -> Iterator[list[str]]:
    """Every warning that logger emits, whatever the rest of the run did to it.

    Not `caplog`. `test_migrations` in `tests/persistence` builds an Alembic
    `Config`, and `fileConfig(disable_existing_loggers=True)` takes pytest's
    capturing handler off the root logger *and disables every logger that
    already exists* -- for the rest of the process. A `caplog` assertion here
    therefore passes when this file runs alone and is vacuous in the run CI
    does, which is exactly how this test reached CI green locally and red there.
    `test_task_ready_listener.py` documents the same trap and solves it by
    asserting something that cannot be switched off; this does the same, by
    holding its own handler and undoing the disable for the duration.
    """

    logger = logging.getLogger(name)
    captured: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = _Collect(level=logging.WARNING)
    disabled = logger.disabled
    level = logger.level
    logger.disabled = False
    logger.setLevel(logging.WARNING)
    logger.addHandler(handler)
    try:
        yield captured
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
        logger.disabled = disabled


TENANT = "tenant_a"
OWNER = "user_1"
NEIGHBOUR = "user_2"
HEADERS = {
    "x-tenant-id": TENANT,
    "x-principal-id": OWNER,
    "Idempotency-Key": "code-1",
}


#: The real thing, because the service puts it straight into an
#: `AgentRunRequest` and pydantic will not take a look-alike.
PRINCIPAL = PrincipalContext(
    principal_id=OWNER, tenant_id=TENANT, scopes=("workspace:write",)
)
NEIGHBOUR_PRINCIPAL = PrincipalContext(principal_id=NEIGHBOUR, tenant_id=TENANT)


class _StubPrincipals:
    def __init__(self, principal: object) -> None:
        self._principal = principal

    def resolve(self, request: object) -> object:
        return self._principal


class _Executor:
    """Returns a finished turn, or holds until it is let go."""

    def __init__(self, *, hold: bool = False) -> None:
        self.release = asyncio.Event()
        self.hold = hold
        self.runs = 0

    async def run(self, request: Any, emit: Any, cancellation: Any) -> AgentOutcome:
        self.runs += 1
        if self.hold:
            await self.release.wait()
        return AgentOutcome(
            agent_run_id=request.trace.agent_run_id,
            status="completed",
            stop_reason="completed",
            output_text="Wrote notes.md.",
        )


class _Publishing:
    """An executor that tries to publish an answer, which Code has none of."""

    def __init__(self) -> None:
        self.runs = 0

    async def run(self, request: Any, emit: Any, cancellation: Any) -> AgentOutcome:
        self.runs += 1
        await emit.emit(UngroundedAnswerCommitted(text="an answer"))
        raise AssertionError("unreachable")


class _Writing:
    """An executor that writes a file the way a tool would: through the scope.

    Seeding the artifact store directly would be shorter and would prove less.
    What is under test is that the endpoint reads the version recorded on the
    session row, and that row only moves because a write moved it -- so the
    write has to be a real one, made where the tools make theirs.
    """

    def __init__(self, scope: WorkspaceScope, name: str = "notes.md") -> None:
        self.scope = scope
        self.name = name

    async def run(self, request: Any, emit: Any, cancellation: Any) -> AgentOutcome:
        session = self.scope.current()
        assert session is not None, "the turn should have entered a workspace"
        session.version = await session.workspace.write(
            session.version,
            self.name,
            b"- ship it\n",
            media_type="text/markdown",
        )
        return AgentOutcome(
            agent_run_id=request.trace.agent_run_id,
            status="completed",
            stop_reason="completed",
            output_text=f"Wrote {self.name}.",
        )


class _World:
    def __init__(
        self,
        *,
        executor: _Executor | None = None,
        max_concurrent_turns: int = 4,
        principal: object | None = None,
        serves_code: bool = True,
        sandbox: object | None = None,
    ) -> None:
        self.conversations = InMemoryConversationStore()
        # Held rather than built inline: an executor that writes files has to
        # reach the same scope the service enters, which is how a real tool
        # finds the working set.
        self.scope = WorkspaceScope()
        self.executor = executor if executor is not None else _Executor()
        self.approvals = CodeApprovalRegistry()
        self.service = CodeSessionService(
            conversations=self.conversations,
            artifacts=InMemoryArtifactStore(),
            executor_for=lambda _scope: self.executor,  # pyright: ignore[reportArgumentType]
            scope=self.scope,
            budget=RunBudget(max_steps=4, max_tool_calls=4),
            turn_timeout_seconds=60,
            max_concurrent_turns=max_concurrent_turns,
            clock=lambda: datetime.now(UTC),
        )
        self.log = InMemoryEventLog()
        self.app = FastAPI()
        if serves_code:
            self.app.include_router(code_route.router)
        setattr(
            self.app.state,
            STATE_ATTRIBUTE,
            SimpleNamespace(
                principals=_StubPrincipals(
                    principal if principal is not None else PRINCIPAL
                ),
                code=self.service if serves_code else None,
                code_approvals=self.approvals if serves_code else None,
                # `None` unless a test asks for one, which is the shape a
                # deployment with `code.sandbox_enabled` off actually has.
                code_sandbox=(
                    None if sandbox is None else SimpleNamespace(runner=sandbox)
                ),
                events=self.log,
                live_events=LiveEventChannel(
                    buffer_events=8, max_subscribers_per_stream=4
                ),
                sink_for=lambda *, stream_id, run_id: _sink(
                    self.log, stream_id, run_id
                ),
                config=SimpleNamespace(
                    sse_heartbeat_seconds=600,
                    chat_recovery=SimpleNamespace(disconnect_poll_seconds=60.0),
                    event_stream=SimpleNamespace(
                        catchup_poll_seconds=1,
                        replay_page_size=500,
                        live_delta_coalesce_ms=10,
                    ),
                ),
            ),
        )
        # Taken from the real mapping rather than restated, so a test cannot
        # keep asserting a status the application stopped answering with.
        for failure in (
            NotFoundError,
            ToolInputInvalidError,
            CodeTurnBusyError,
            CodeCapacityError,
            CodeRunNotPermittedError,
            CodeRunRefusedError,
            CodeRunUnavailableError,
            ApprovalNotPendingError,
            StandingApprovalRefusedError,
        ):
            self.app.add_exception_handler(
                failure,
                _refuse(ERROR_STATUS[failure]),  # pyright: ignore[reportArgumentType]
            )


def _sink(log: InMemoryEventLog, stream_id: str, run_id: str) -> Any:
    from agent_workbench.adapters.events import ScopedEventSink

    return ScopedEventSink(
        log=log, scope=EventScope(stream_id=stream_id, run_id=run_id)
    )


def _refuse(status: int) -> Any:
    def handler(_request: Any, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    return handler


def _run(world: _World, scenario: Any) -> Any:
    async def execute() -> Any:
        transport = httpx.ASGITransport(app=world.app)  # pyright: ignore[reportArgumentType]
        async with httpx.AsyncClient(
            transport=transport, base_url="http://api.test"
        ) as client:
            return await scenario(client)

    return asyncio.run(execute())


def _opened(client: httpx.AsyncClient) -> Any:
    return client.post(f"{code_route.CODE_PREFIX}/sessions", headers=HEADERS, json={})


def test_a_session_takes_an_instruction_and_answers_with_a_report() -> None:
    """The end-to-end shape, over HTTP: open, ask, read it back."""

    world = _World()

    async def scenario(client: httpx.AsyncClient) -> tuple[int, str, list[str]]:
        created = await _opened(client)
        session_id = created.json()["session_id"]
        answered = await client.post(
            f"{code_route.CODE_PREFIX}/sessions/{session_id}/messages",
            headers=HEADERS,
            json={"instruction": "write notes.md"},
        )
        history = await client.get(
            f"{code_route.CODE_PREFIX}/sessions/{session_id}/messages",
            headers=HEADERS,
        )
        return (
            answered.status_code,
            answered.json()["report"],
            [message["role"] for message in history.json()["messages"]],
        )

    status, report, roles = _run(world, scenario)

    assert status == 200
    assert report == "Wrote notes.md."
    assert roles == ["user", "assistant"]


def test_a_second_turn_on_a_busy_session_is_a_conflict() -> None:
    world = _World(executor=_Executor(hold=True))

    async def scenario(client: httpx.AsyncClient) -> tuple[int, int]:
        created = await _opened(client)
        session_id = created.json()["session_id"]
        path = f"{code_route.CODE_PREFIX}/sessions/{session_id}/messages"
        first = asyncio.ensure_future(
            client.post(path, headers=HEADERS, json={"instruction": "one"})
        )
        await asyncio.sleep(0.05)
        second = await client.post(
            path,
            headers={**HEADERS, "Idempotency-Key": "code-2"},
            json={"instruction": "two"},
        )
        world.executor.release.set()
        await first
        return second.status_code, world.executor.runs

    status, runs = _run(world, scenario)

    assert status == 409
    # Refused before it reached the executor, not after it had started work.
    assert runs == 1


def test_a_process_at_capacity_says_come_back() -> None:
    world = _World(executor=_Executor(hold=True), max_concurrent_turns=1)

    async def scenario(client: httpx.AsyncClient) -> int:
        first_session = (await _opened(client)).json()["session_id"]
        second_session = (await _opened(client)).json()["session_id"]
        first = asyncio.ensure_future(
            client.post(
                f"{code_route.CODE_PREFIX}/sessions/{first_session}/messages",
                headers=HEADERS,
                json={"instruction": "one"},
            )
        )
        await asyncio.sleep(0.05)
        second = await client.post(
            f"{code_route.CODE_PREFIX}/sessions/{second_session}/messages",
            headers={**HEADERS, "Idempotency-Key": "code-2"},
            json={"instruction": "two"},
        )
        world.executor.release.set()
        await first
        return second.status_code

    # 429, not 409: a different session, so this is the process saying it is
    # full rather than the session saying it is busy.
    assert _run(world, scenario) == 429


def test_a_chat_session_is_not_addressable_here() -> None:
    world = _World()

    async def scenario(client: httpx.AsyncClient) -> tuple[int, int]:
        await world.conversations.create_session(
            session_id="ses_chat_1", tenant_id=TENANT, owner_id=OWNER
        )
        asked = await client.post(
            f"{code_route.CODE_PREFIX}/sessions/ses_chat_1/messages",
            headers=HEADERS,
            json={"instruction": "write notes.md"},
        )
        read = await client.get(
            f"{code_route.CODE_PREFIX}/sessions/ses_chat_1/messages", headers=HEADERS
        )
        return asked.status_code, read.status_code

    assert _run(world, scenario) == (404, 404)


def test_another_principal_gets_the_same_answer_as_a_stranger() -> None:
    """Opened by its owner, addressed by somebody else."""

    owner_world = _World()

    async def opened() -> str:
        return await owner_world.service.open(tenant_id=TENANT, principal_id=OWNER)

    session_id = asyncio.run(opened())
    world = _World(principal=NEIGHBOUR_PRINCIPAL)
    world.service.conversations = owner_world.conversations

    async def scenario(client: httpx.AsyncClient) -> int:
        answered = await client.post(
            f"{code_route.CODE_PREFIX}/sessions/{session_id}/messages",
            headers=HEADERS,
            json={"instruction": "write notes.md"},
        )
        return answered.status_code

    assert _run(world, scenario) == 404


def test_a_process_that_runs_no_coding_turns_has_no_code_routes() -> None:
    """The router is absent, not present-and-broken."""

    world = _World(serves_code=False)

    async def scenario(client: httpx.AsyncClient) -> int:
        created = await _opened(client)
        return created.status_code

    assert _run(world, scenario) == 404


def test_an_approval_that_is_not_yours_does_not_exist() -> None:
    """And a second decision finds nothing pending, because the first removed it."""

    world = _World()
    scope = ApprovalScope(tenant_id=TENANT, session_id="ses_code_1", principal_id=OWNER)

    async def scenario(client: httpx.AsyncClient) -> tuple[int, int]:
        gate = world.approvals.gate_for(scope)
        held = asyncio.ensure_future(
            gate.request(
                approval_id="apr_1",
                tool_call_id="toolu_1",
                tool_name="workspace_write",
                argument_digest="a" * 64,
                risk="write",
                required_scopes=(),
                timeout_seconds=5.0,
            )
        )
        await asyncio.sleep(0)
        stranger = await client.post(
            f"{code_route.CODE_PREFIX}/sessions/ses_other/approvals/apr_1",
            headers=HEADERS,
            json={"decision": "approve_once"},
        )
        await client.post(
            f"{code_route.CODE_PREFIX}/sessions/ses_code_1/approvals/apr_1",
            headers=HEADERS,
            json={"decision": "approve_once"},
        )
        await asyncio.wait_for(held, timeout=5.0)
        again = await client.post(
            f"{code_route.CODE_PREFIX}/sessions/ses_code_1/approvals/apr_1",
            headers=HEADERS,
            json={"decision": "approve_once"},
        )
        return stranger.status_code, again.status_code

    stranger, again = _run(world, scenario)

    assert stranger == 404
    # Not 409: resolving removed it, so the second decision finds no such
    # question -- which is the same thing a stranger is told.
    assert again == 404


def test_two_decisions_in_the_same_breath_do_not_both_land() -> None:
    """The window between resolving a question and the waiter noticing.

    The gate withdraws its own question when it wakes, so a second decision
    after that is already answered with "no such approval". This is the other
    case: two decisions with nothing awaited in between, which is what two
    clicks on one button look like.
    """

    world = _World()
    scope = ApprovalScope(tenant_id=TENANT, session_id="ses_code_1", principal_id=OWNER)

    async def scenario(client: httpx.AsyncClient) -> tuple[int, int]:
        gate = world.approvals.gate_for(scope)
        held = asyncio.ensure_future(
            gate.request(
                approval_id="apr_1",
                tool_call_id="toolu_1",
                tool_name="workspace_write",
                argument_digest="a" * 64,
                risk="write",
                required_scopes=(),
                timeout_seconds=5.0,
            )
        )
        await asyncio.sleep(0)
        path = f"{code_route.CODE_PREFIX}/sessions/ses_code_1/approvals/apr_1"
        body = {"decision": "approve_once"}
        first, second = await asyncio.gather(
            client.post(path, headers=HEADERS, json=body),
            client.post(path, headers=HEADERS, json=body),
        )
        await asyncio.wait_for(held, timeout=5.0)
        return first.status_code, second.status_code

    first, second = _run(world, scenario)

    assert first == 200
    # Not 409 "already decided": removing it in the same breath as resolving it
    # means the second request finds no such question at all.
    assert second == 404


def test_a_standing_yes_is_refused_for_an_external_tool() -> None:
    """A blanket yes to an irreversible effect is what must be asked each time."""

    world = _World()
    scope = ApprovalScope(tenant_id=TENANT, session_id="ses_code_1", principal_id=OWNER)

    async def scenario(client: httpx.AsyncClient) -> tuple[int, int]:
        gate = world.approvals.gate_for(scope)
        held = asyncio.ensure_future(
            gate.request(
                approval_id="apr_1",
                tool_call_id="toolu_1",
                tool_name="sandbox_run",
                argument_digest="a" * 64,
                risk="external",
                required_scopes=(),
                timeout_seconds=5.0,
            )
        )
        await asyncio.sleep(0)
        standing = await client.post(
            f"{code_route.CODE_PREFIX}/sessions/ses_code_1/approvals/apr_1",
            headers=HEADERS,
            json={"decision": "approve_for_session"},
        )
        once = await client.post(
            f"{code_route.CODE_PREFIX}/sessions/ses_code_1/approvals/apr_1",
            headers=HEADERS,
            json={"decision": "approve_once"},
        )
        await asyncio.wait_for(held, timeout=5.0)
        return standing.status_code, once.status_code

    standing, once = _run(world, scenario)

    assert standing == 422
    # The control: the same call is approvable, just not for the session.
    assert once == 200


def test_the_pending_list_shows_what_a_session_is_stopped_on() -> None:
    world = _World()
    scope = ApprovalScope(tenant_id=TENANT, session_id="ses_code_1", principal_id=OWNER)

    async def scenario(client: httpx.AsyncClient) -> tuple[list[str], int]:
        gate = world.approvals.gate_for(scope)
        held = asyncio.ensure_future(
            gate.request(
                approval_id="apr_1",
                tool_call_id="toolu_1",
                tool_name="workspace_write",
                argument_digest="a" * 64,
                risk="write",
                required_scopes=(),
                timeout_seconds=5.0,
            )
        )
        await asyncio.sleep(0)
        listed = await client.get(
            f"{code_route.CODE_PREFIX}/sessions/ses_code_1/approvals", headers=HEADERS
        )
        other = await client.get(
            f"{code_route.CODE_PREFIX}/sessions/ses_other/approvals", headers=HEADERS
        )
        held.cancel()
        return (
            [held["tool_name"] for held in listed.json()["approvals"]],
            len(other.json()["approvals"]),
        )

    names, others = _run(world, scenario)

    assert names == ["workspace_write"]
    # Another session sees none of it, which is the same scoping the decision
    # endpoint enforces.
    assert others == 0


def test_the_status_codes_come_from_the_application_table() -> None:
    """A test restating them would keep passing after the application moved."""

    assert ERROR_STATUS[CodeTurnBusyError] == 409
    assert ERROR_STATUS[CodeCapacityError] == 429
    assert ERROR_STATUS[StandingApprovalRefusedError] == 422
    assert cast(int, ERROR_STATUS[NotFoundError]) == 404


# --- the real assembly ---------------------------------------------------
#
# Everything above builds the router by hand, which proves what the routes do
# and nothing about whether a deployment ever gets them. These two boot the
# actual application from a config file.


def _assembled_settings(
    root: Path, *, code_enabled: bool, model_pinned: bool = True
) -> Settings:
    with DEFAULT_CONFIG_FILE.open("rb") as handle:
        payload: dict[str, Any] = tomllib.load(handle)
    dsn = os.environ["AGENT_WORKBENCH_TEST_DSN"]
    payload["database"].update(dsn=dsn, guard_dsn=dsn, listen_dsn=dsn)
    # Left as the shipped `not-configured-*` placeholders when a test wants the
    # deployment mistake rather than a working one. That is the real shape of
    # it: an overlay that turns Code on and forgets that the ids underneath it
    # are placeholders `build_model` refuses.
    if model_pinned:
        payload["model"]["main"]["model_id"] = "deepseek-chat"
        payload["model"]["compact"]["model_id"] = "deepseek-chat"
    payload["artifact_store"]["local_root"] = str(root)
    payload["secrets"] = {"deepseek_api_key": "sk-unit-test"}
    payload["code"] = {**payload["code"], "enabled": code_enabled}
    return Settings(**payload)


def _booted(
    root: Path, *, code_enabled: bool, model_pinned: bool = True
) -> tuple[bool, int, str]:
    async def execute() -> tuple[bool, int, str]:
        dependencies = build_dependencies(
            project_api(
                _assembled_settings(
                    root, code_enabled=code_enabled, model_pinned=model_pinned
                )
            )
        )
        app = create_app(dependencies)
        transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://api.test"
            ) as client:
                opened = await client.post(
                    f"{code_route.CODE_PREFIX}/sessions",
                    headers={"x-tenant-id": TENANT, "x-principal-id": OWNER},
                    json={},
                )
            # The status alone cannot tell "no such route" from "the route
            # refused": both are 404. The body can -- an unrouted path is
            # answered by the framework's own default, and a mounted route
            # that refuses is answered by this application's handler.
            return (
                dependencies.serves_code,
                opened.status_code,
                str(opened.json().get("detail", "")),
            )
        finally:
            await dependencies.dispose()

    return asyncio.run(execute())


@pytest.mark.skipif(
    "AGENT_WORKBENCH_TEST_DSN" not in os.environ,
    reason="the real assembly needs a database",
)
def test_a_deployment_that_asks_for_code_gets_the_routes(tmp_path: Path) -> None:
    """Boots the actual application, from the shipped config plus one flag.

    The tests above build the router by hand, so they would all keep passing
    in a deployment where nothing ever mounted it. This is the one that says a
    coding session is reachable.
    """

    serves, status, _ = _booted(tmp_path, code_enabled=True)

    assert serves is True
    assert status == 201


@pytest.mark.skipif(
    "AGENT_WORKBENCH_TEST_DSN" not in os.environ,
    reason="the real assembly needs a database",
)
def test_the_shipped_default_does_not_mount_them(tmp_path: Path) -> None:
    """The control, and the shipped state: off until somebody asks."""

    serves, status, detail = _booted(tmp_path, code_enabled=False)

    assert serves is False
    assert status == 404
    # Absent, not present-and-refusing. Both answer 404, so the status alone
    # cannot tell them apart, and "mounted unconditionally" is exactly the
    # mistake that would look right from outside until the day the service was
    # there and the flag said no. The framework's own default body is what
    # says no route matched.
    assert detail == "Not Found"


def test_a_turn_cannot_publish_an_answer_through_the_route_either() -> None:
    """The fence is what the route hands the service, not what it hopes for.

    Everything else here uses an executor that never tries. This one does, and
    the assertion is that it fails -- because the sink the route builds has no
    publication methods and refuses the event outright.
    """

    world = _World(executor=cast(Any, _Publishing()))

    async def scenario(client: httpx.AsyncClient) -> tuple[bool, list[str]]:
        session_id = (await _opened(client)).json()["session_id"]
        try:
            await client.post(
                f"{code_route.CODE_PREFIX}/sessions/{session_id}/messages",
                headers=HEADERS,
                json={"instruction": "answer me"},
            )
            answered = True
        except RuntimeError:
            answered = False
        stored = await world.log.read(session_id)
        return answered, [envelope.event_type for envelope in stored]

    answered, events = _run(world, scenario)

    assert answered is False
    assert "UngroundedAnswerCommitted" not in events


def test_the_workspace_endpoint_lists_what_the_turn_wrote() -> None:
    """The product of a coding session, without spending a turn to see it."""

    world = _World()
    world.executor = _Writing(world.scope)

    async def scenario(client: httpx.AsyncClient) -> tuple[list[Any], list[Any]]:
        created = await _opened(client)
        session_id = created.json()["session_id"]
        before = await client.get(
            f"{code_route.CODE_PREFIX}/sessions/{session_id}/workspace",
            headers=HEADERS,
        )
        await client.post(
            f"{code_route.CODE_PREFIX}/sessions/{session_id}/messages",
            headers=HEADERS,
            json={"instruction": "write notes.md"},
        )
        after = await client.get(
            f"{code_route.CODE_PREFIX}/sessions/{session_id}/workspace",
            headers=HEADERS,
        )
        return before.json()["files"], after.json()["files"]

    before, after = _run(world, scenario)

    # The empty read is the control. Without it "the file is listed" would also
    # pass against an endpoint that listed every artifact this principal owns.
    assert before == []
    assert [(entry["name"], entry["media_type"]) for entry in after] == [
        ("notes.md", "text/markdown")
    ]
    assert after[0]["size_bytes"] == len(b"- ship it\n")


def test_a_chat_session_has_no_workspace_to_show() -> None:
    """The same gate the rest of this router is behind, on the new endpoint."""

    world = _World()

    async def scenario(client: httpx.AsyncClient) -> int:
        await world.conversations.create_session(
            session_id="ses_chat_1", tenant_id=TENANT, owner_id=OWNER
        )
        read = await client.get(
            f"{code_route.CODE_PREFIX}/sessions/ses_chat_1/workspace", headers=HEADERS
        )
        return read.status_code

    assert _run(world, scenario) == 404


def test_another_principal_cannot_read_the_working_set() -> None:
    """Files are the product, so this is the read that would leak the work."""

    owner_world = _World()
    owner_world.executor = _Writing(owner_world.scope)

    async def write_one(client: httpx.AsyncClient) -> str:
        created = await _opened(client)
        session_id = created.json()["session_id"]
        await client.post(
            f"{code_route.CODE_PREFIX}/sessions/{session_id}/messages",
            headers=HEADERS,
            json={"instruction": "write notes.md"},
        )
        return session_id

    session_id = _run(owner_world, write_one)

    world = _World(principal=NEIGHBOUR_PRINCIPAL)
    # Both stores, so the refusal is the session gate rather than an empty
    # store the neighbour would have found nothing in anyway.
    world.service.conversations = owner_world.conversations
    world.service.artifacts = owner_world.service.artifacts

    async def scenario(client: httpx.AsyncClient) -> int:
        read = await client.get(
            f"{code_route.CODE_PREFIX}/sessions/{session_id}/workspace", headers=HEADERS
        )
        return read.status_code

    assert _run(world, scenario) == 404


@pytest.mark.skipif(
    "AGENT_WORKBENCH_TEST_DSN" not in os.environ,
    reason="the real assembly needs a database",
)
def test_a_process_that_cannot_serve_code_says_so(tmp_path: Path) -> None:
    """Asked for, not served, and not silent about it.

    Written from a profile that got this wrong. `code.enabled = true` sat two
    lines below model ids the overlay never pinned, `build_model` refused, and
    the coding half went down with it -- while the process printed "startup
    complete" and answered 404 on every /v1/code path. From outside, that is
    the same thing a build without the routes looks like.

    The refusal itself is right: a coding turn is a model loop or it is
    nothing. What was wrong is that it happened two layers below anything with
    the word "code" in it, and nobody upstream heard.
    """

    with _warnings_from(_API_LOGGER) as warnings:
        serves, status, _ = _booted(tmp_path, code_enabled=True, model_pinned=False)

    assert serves is False
    assert status == 404
    assert any("code.enabled is true" in line for line in warnings), warnings


@pytest.mark.skipif(
    "AGENT_WORKBENCH_TEST_DSN" not in os.environ,
    reason="the real assembly needs a database",
)
def test_a_process_that_was_not_asked_for_code_stays_quiet(tmp_path: Path) -> None:
    """The control. Off because nobody asked is not a problem to report."""

    with _warnings_from(_API_LOGGER) as warnings:
        _booted(tmp_path, code_enabled=False, model_pinned=False)

    assert not any("code.enabled is true" in line for line in warnings), warnings


def test_a_workspace_file_can_be_read_back_by_name() -> None:
    """The product of a coding session, as bytes rather than as a row."""

    world = _World()
    world.executor = _Writing(world.scope)

    async def scenario(client: httpx.AsyncClient) -> tuple[int, bytes, str, str, str]:
        created = await _opened(client)
        session_id = created.json()["session_id"]
        await client.post(
            f"{code_route.CODE_PREFIX}/sessions/{session_id}/messages",
            headers=HEADERS,
            json={"instruction": "write notes.md"},
        )
        read = await client.get(
            f"{code_route.CODE_PREFIX}/sessions/{session_id}/workspace/notes.md",
            headers=HEADERS,
        )
        return (
            read.status_code,
            read.content,
            read.headers["content-type"],
            read.headers["content-disposition"],
            read.headers["x-content-type-options"],
        )

    status, body, media_type, disposition, sniffing = _run(world, scenario)

    assert status == 200
    assert body == b"- ship it\n"
    # From the manifest entry the name resolved to, not from a second lookup:
    # headers and body have to describe the same version of the same file.
    assert media_type.startswith("text/markdown")
    assert "notes.md" in disposition
    # The manifest's media type is the whole answer; sniffing would let a
    # browser promote the label.
    assert sniffing == "nosniff"


def test_a_name_the_workspace_does_not_bind_is_not_found() -> None:
    """A `KeyError` escaping here would be a 500, and a 500 is an answer."""

    world = _World()
    world.executor = _Writing(world.scope)

    async def scenario(client: httpx.AsyncClient) -> tuple[int, int]:
        created = await _opened(client)
        session_id = created.json()["session_id"]
        await client.post(
            f"{code_route.CODE_PREFIX}/sessions/{session_id}/messages",
            headers=HEADERS,
            json={"instruction": "write notes.md"},
        )
        missing = await client.get(
            f"{code_route.CODE_PREFIX}/sessions/{session_id}/workspace/absent.md",
            headers=HEADERS,
        )
        # The control. Without it, a route that answered 404 for everything --
        # including the file that exists -- would pass the assertion above.
        present = await client.get(
            f"{code_route.CODE_PREFIX}/sessions/{session_id}/workspace/notes.md",
            headers=HEADERS,
        )
        return missing.status_code, present.status_code

    assert _run(world, scenario) == (404, 200)


def test_another_principal_cannot_download_the_working_set() -> None:
    """The files are the product, so this is the read that would leak the work."""

    owner_world = _World()
    owner_world.executor = _Writing(owner_world.scope)

    async def write_one(client: httpx.AsyncClient) -> str:
        created = await _opened(client)
        session_id = created.json()["session_id"]
        await client.post(
            f"{code_route.CODE_PREFIX}/sessions/{session_id}/messages",
            headers=HEADERS,
            json={"instruction": "write notes.md"},
        )
        return session_id

    session_id = _run(owner_world, write_one)

    world = _World(principal=NEIGHBOUR_PRINCIPAL)
    # Both stores, so the refusal is the session gate rather than a neighbour
    # who would have found nothing in an empty store anyway.
    world.service.conversations = owner_world.conversations
    world.service.artifacts = owner_world.service.artifacts

    async def scenario(client: httpx.AsyncClient) -> int:
        read = await client.get(
            f"{code_route.CODE_PREFIX}/sessions/{session_id}/workspace/notes.md",
            headers=HEADERS,
        )
        return read.status_code

    assert _run(world, scenario) == 404


def test_a_chat_session_has_no_workspace_file_to_hand_over() -> None:
    """The same mode gate the listing is behind, on the endpoint that moves bytes.

    The chat session is given a working set that really does bind the name --
    the very manifest a code session just produced, pointed at by the same
    owner. Without that setup this test passes for the wrong reason: a chat
    session's workspace is empty, so `locate` refuses on the missing name and
    the mode is never consulted. Removing the mode gate then leaves it green,
    which is exactly what happened the first time it was written.
    """

    world = _World()
    world.executor = _Writing(world.scope)

    async def scenario(client: httpx.AsyncClient) -> tuple[int, int]:
        created = await _opened(client)
        code_session = created.json()["session_id"]
        await client.post(
            f"{code_route.CODE_PREFIX}/sessions/{code_session}/messages",
            headers=HEADERS,
            json={"instruction": "write notes.md"},
        )
        written = await world.conversations.session(
            session_id=code_session,
            tenant_id=TENANT,
            principal_id=OWNER,
            mode="code",
        )
        assert written.workspace_version is not None

        await world.conversations.create_session(
            session_id="ses_chat_1", tenant_id=TENANT, owner_id=OWNER
        )
        await world.conversations.advance_workspace_version(
            session_id="ses_chat_1",
            tenant_id=TENANT,
            principal_id=OWNER,
            expected=None,
            next_version=written.workspace_version,
        )

        refused = await client.get(
            f"{code_route.CODE_PREFIX}/sessions/ses_chat_1/workspace/notes.md",
            headers=HEADERS,
        )
        # The control. The same bytes, the same owner, the same manifest -- so
        # the only thing that can separate these two answers is the mode.
        served = await client.get(
            f"{code_route.CODE_PREFIX}/sessions/{code_session}/workspace/notes.md",
            headers=HEADERS,
        )
        return refused.status_code, served.status_code

    assert _run(world, scenario) == (404, 200)


def test_the_first_instruction_names_the_session() -> None:
    """A session is opened before there is anything to call it."""

    world = _World()

    async def scenario(client: httpx.AsyncClient) -> tuple[str | None, str | None]:
        created = await _opened(client)
        session_id = created.json()["session_id"]
        before = created.json()["title"]
        await client.post(
            f"{code_route.CODE_PREFIX}/sessions/{session_id}/messages",
            headers=HEADERS,
            json={"instruction": "把 notes.md 里的待办整理成清单"},
        )
        listed = await client.get(f"{code_route.CODE_PREFIX}/sessions", headers=HEADERS)
        return before, listed.json()["sessions"][0]["title"]

    before, after = _run(world, scenario)

    assert before is None
    assert after == "把 notes.md 里的待办整理成清单"


def test_a_second_instruction_does_not_rename_the_session() -> None:
    """First one wins. A name that drifted with every turn would not be a name."""

    world = _World()

    async def scenario(client: httpx.AsyncClient) -> str | None:
        created = await _opened(client)
        session_id = created.json()["session_id"]
        for turn, instruction in enumerate(("第一句", "第二句")):
            await client.post(
                f"{code_route.CODE_PREFIX}/sessions/{session_id}/messages",
                # A header is ASCII, so the key is derived from the position
                # rather than from the instruction it carries.
                headers={**HEADERS, "Idempotency-Key": f"code-turn-{turn}"},
                json={"instruction": instruction},
            )
        listed = await client.get(f"{code_route.CODE_PREFIX}/sessions", headers=HEADERS)
        return listed.json()["sessions"][0]["title"]

    assert _run(world, scenario) == "第一句"


def test_a_renamed_session_keeps_the_name_the_person_gave_it() -> None:
    """The one overwrite, and it survives the turns that follow it."""

    world = _World()

    async def scenario(client: httpx.AsyncClient) -> tuple[str | None, str | None]:
        created = await _opened(client)
        session_id = created.json()["session_id"]
        await client.post(
            f"{code_route.CODE_PREFIX}/sessions/{session_id}/messages",
            headers=HEADERS,
            json={"instruction": "第一句"},
        )
        renamed = await client.patch(
            f"{code_route.CODE_PREFIX}/sessions/{session_id}",
            headers=HEADERS,
            json={"title": "重构工作区"},
        )
        await client.post(
            f"{code_route.CODE_PREFIX}/sessions/{session_id}/messages",
            headers={**HEADERS, "Idempotency-Key": "code-after-rename"},
            json={"instruction": "第三句"},
        )
        listed = await client.get(f"{code_route.CODE_PREFIX}/sessions", headers=HEADERS)
        return renamed.json()["title"], listed.json()["sessions"][0]["title"]

    assert _run(world, scenario) == ("重构工作区", "重构工作区")


def test_the_session_list_does_not_show_another_principal_s_sessions() -> None:
    owner_world = _World()

    async def open_one(client: httpx.AsyncClient) -> str:
        created = await _opened(client)
        return str(created.json()["session_id"])

    session_id = _run(owner_world, open_one)

    world = _World(principal=NEIGHBOUR_PRINCIPAL)
    world.service.conversations = owner_world.conversations

    async def scenario(client: httpx.AsyncClient) -> tuple[list[str], int]:
        listed = await client.get(f"{code_route.CODE_PREFIX}/sessions", headers=HEADERS)
        named = await client.patch(
            f"{code_route.CODE_PREFIX}/sessions/{session_id}",
            headers=HEADERS,
            json={"title": "mine now"},
        )
        return (
            [row["session_id"] for row in listed.json()["sessions"]],
            named.status_code,
        )

    assert _run(world, scenario) == ([], 404)


def test_a_chat_session_cannot_be_renamed_through_the_code_api() -> None:
    world = _World()

    async def scenario(client: httpx.AsyncClient) -> tuple[int, list[str]]:
        await world.conversations.create_session(
            session_id="ses_chat_1", tenant_id=TENANT, owner_id=OWNER
        )
        refused = await client.patch(
            f"{code_route.CODE_PREFIX}/sessions/ses_chat_1",
            headers=HEADERS,
            json={"title": "reached through the wrong door"},
        )
        # And it is not in the list either, which is the same gate read the
        # other way round: one table, two APIs, and the mode is the door.
        listed = await client.get(f"{code_route.CODE_PREFIX}/sessions", headers=HEADERS)
        return (
            refused.status_code,
            [row["session_id"] for row in listed.json()["sessions"]],
        )

    assert _run(world, scenario) == (404, [])


# --- Running one file out of the working set (ADR-065) ------------------------
#
# The sandbox itself is a container this suite must not start, so what stands in
# for it is an `MCPClientPort` returning the envelope the real server returns.
# That keeps the *whole* project-side path under test -- reading inputs out of
# the workspace, the entry script, binding outputs back to versions -- which is
# the half that can be wrong here. `tests/mcp` covers the server's own contract.


PRINCIPAL_WITH_SANDBOX = PrincipalContext(
    principal_id=OWNER, tenant_id=TENANT, scopes=("workspace:write", "sandbox:run")
)


class _FakeSandboxServer:
    """One canned ``run_python`` result, and a record of what it was asked."""

    def __init__(
        self,
        *,
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        outputs: tuple[tuple[str, bytes], ...] = (),
        is_error: bool = False,
        message: str = "",
    ) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.outputs = outputs
        self.is_error = is_error
        self.message = message
        self.calls: list[dict[str, Any]] = []

    async def list_tools_page(self, cursor: str | None) -> Any:  # pragma: no cover
        raise AssertionError("the runner never lists tools")

    async def call_tool(self, name: str, arguments: Any) -> Any:
        self.calls.append(cast(dict[str, Any], arguments))
        if self.is_error:
            return SimpleNamespace(
                content=(SimpleNamespace(text=self.message),),
                structured_content=None,
                is_error=True,
            )
        return SimpleNamespace(
            content=(),
            structured_content={
                "exit_code": self.exit_code,
                "stdout": self.stdout,
                "stderr": self.stderr,
                "outputs": [
                    {
                        "name": output_name,
                        "content_base64": base64.b64encode(body).decode("ascii"),
                    }
                    for output_name, body in self.outputs
                ],
            },
            is_error=False,
        )


def _ran(world: _World, session_id: str, name: str) -> str:
    return f"{code_route.CODE_PREFIX}/sessions/{session_id}/workspace/{name}/run"


def _world_that_wrote(
    server: _FakeSandboxServer | None,
    *,
    name: str = "sq.py",
    principal: object | None = None,
) -> tuple[_World, Any]:
    """A world holding one file a turn produced, and its sandbox if it has one."""

    world = _World(
        executor=None,
        principal=principal if principal is not None else PRINCIPAL_WITH_SANDBOX,
        sandbox=None if server is None else WorkspaceSandbox(client=server),  # pyright: ignore[reportArgumentType]
    )
    writer = _Writing(world.scope, name=name)
    world.executor = writer
    world.service.executor_for = lambda _scope: writer  # pyright: ignore[reportArgumentType]
    return world, writer


async def _session_with_file(client: httpx.AsyncClient) -> str:
    created = await _opened(client)
    session_id = cast(str, created.json()["session_id"])
    await client.post(
        f"{code_route.CODE_PREFIX}/sessions/{session_id}/messages",
        headers=HEADERS,
        json={"instruction": "write it"},
    )
    return session_id


def test_running_a_file_returns_what_the_sandbox_said() -> None:
    """The whole point: a `.py` the reader can see, run, without a model turn."""

    server = _FakeSandboxServer(exit_code=0, stdout="1\n4\n9\n")
    world, _ = _world_that_wrote(server)

    async def scenario(client: httpx.AsyncClient) -> tuple[int, Any]:
        session_id = await _session_with_file(client)
        answered = await client.post(_ran(world, session_id, "sq.py"), headers=HEADERS)
        return answered.status_code, answered.json()

    status, body = _run(world, scenario)

    assert status == 200
    assert body["exit_code"] == 0
    assert body["stdout"] == "1\n4\n9\n"
    assert body["written"] == []
    assert body["omitted_inputs"] == []
    # `runpy`, not the file's own source pasted in as the script: the sandbox
    # writes whatever it is given to `/sandbox/script.py`, so a pasted body
    # raises from a filename the reader has never seen.
    script = cast(str, server.calls[0]["script"])
    assert "runpy.run_path('sq.py'" in script
    # The two lines that look like boilerplate and are not. Both were measured
    # against a real container, and a fake client cannot fail the way either
    # one failed -- so what is left to pin is that they are still being sent.
    # `sys.path`: `run_path` does not touch it and `python -I` implies `-P`, so
    # without this an `import helper` beside the file raises ModuleNotFoundError.
    # `dont_write_bytecode`: that import then writes `__pycache__/`, and the
    # sandbox refuses a directory in its flat output -- the run is lost at
    # collection time, after the script already succeeded.
    assert 'sys.path.insert(0, "")' in script
    assert "sys.dont_write_bytecode = True" in script
    # And the file itself went in, because that is what `run_path` opens.
    assert [entry["name"] for entry in server.calls[0]["inputs"]] == ["sq.py"]


def test_a_file_the_script_wrote_lands_in_the_working_set() -> None:
    """Files out, the same way a tool call's are -- one version per write."""

    server = _FakeSandboxServer(stdout="done\n", outputs=(("out.csv", b"a,b\n1,2\n"),))
    world, _ = _world_that_wrote(server)

    async def scenario(client: httpx.AsyncClient) -> tuple[Any, list[str]]:
        session_id = await _session_with_file(client)
        answered = await client.post(_ran(world, session_id, "sq.py"), headers=HEADERS)
        listed = await client.get(
            f"{code_route.CODE_PREFIX}/sessions/{session_id}/workspace", headers=HEADERS
        )
        return answered.json(), [row["name"] for row in listed.json()["files"]]

    body, names = _run(world, scenario)

    assert body["written"] == ["out.csv"]
    # The working set as it stands after the run, in the same response. Without
    # it a console that wants to *show* `out.csv` has only its name, and has to
    # re-read the listing on a request that races the response it is reacting
    # to -- which for one render left every produced file as a line of text
    # (known-gaps F-15). The whole set rather than just the written names, for
    # the reason the PUT route already gives: the caller's next question is
    # always "what is in there now".
    assert {entry["name"] for entry in body["files"]} == {"sq.py", "out.csv"}
    produced = next(entry for entry in body["files"] if entry["name"] == "out.csv")
    assert produced["media_type"] == "text/csv"
    assert produced["size_bytes"] == len(b"a,b\n1,2\n")
    # Read off the session the run advanced, so a caller can refresh without a
    # second question -- and the listing agrees, which is the part that proves
    # the pointer moved on the session row rather than only in memory.
    assert body["workspace_version"] is not None
    assert sorted(names) == ["out.csv", "sq.py"]


def test_a_deployment_without_a_sandbox_says_so_rather_than_404() -> None:
    """503 and the setting to turn on. A 404 would read as "no such file"."""

    world, _ = _world_that_wrote(None)

    async def scenario(client: httpx.AsyncClient) -> tuple[int, str]:
        session_id = await _session_with_file(client)
        refused = await client.post(_ran(world, session_id, "sq.py"), headers=HEADERS)
        return refused.status_code, cast(str, refused.json()["detail"])

    status, detail = _run(world, scenario)

    assert status == 503
    assert "sandbox_enabled" in detail


def test_running_a_file_needs_the_same_scope_the_tool_needs() -> None:
    """No Policy Gateway on this path, so the route is the gate."""

    server = _FakeSandboxServer()
    world, _ = _world_that_wrote(server, principal=PRINCIPAL)

    async def scenario(client: httpx.AsyncClient) -> tuple[int, int]:
        session_id = await _session_with_file(client)
        refused = await client.post(_ran(world, session_id, "sq.py"), headers=HEADERS)
        return refused.status_code, len(server.calls)

    assert _run(world, scenario) == (403, 0)


def test_a_name_that_is_not_python_is_refused_before_the_container() -> None:
    """Otherwise a `.md` reaches the sandbox and comes back a SyntaxError."""

    server = _FakeSandboxServer()
    world, _ = _world_that_wrote(server, name="notes.md")

    async def scenario(client: httpx.AsyncClient) -> tuple[int, int]:
        session_id = await _session_with_file(client)
        refused = await client.post(
            _ran(world, session_id, "notes.md"), headers=HEADERS
        )
        return refused.status_code, len(server.calls)

    assert _run(world, scenario) == (422, 0)


def test_a_python_file_is_recognised_by_its_type_when_the_name_is_silent() -> None:
    """An upload keeps whatever its browser guessed; the console strips the
    same parameter before offering a 运行 button, and a server that did not
    would refuse the file its client had just offered one for."""

    server = _FakeSandboxServer(stdout="ok\n")
    world, _ = _world_that_wrote(server, name="notes.md")

    async def scenario(client: httpx.AsyncClient) -> int:
        session_id = await _session_with_file(client)
        await client.put(
            f"{code_route.CODE_PREFIX}/sessions/{session_id}/workspace/script",
            headers={**HEADERS, "content-type": "text/x-python; charset=utf-8"},
            content=b"print('hi')\n",
        )
        answered = await client.post(_ran(world, session_id, "script"), headers=HEADERS)
        return answered.status_code

    assert _run(world, scenario) == 200


def test_an_upload_whose_header_is_upper_case_is_stored_not_rejected() -> None:
    """RFC 9110 says media types are case-insensitive; this route did not.

    ``MediaType`` is ``^[a-z]+/...`` (``domain/artifacts.py``), so a client
    sending ``Content-Type: TEXT/PLAIN`` -- entirely legal, and what some HTTP
    clients emit -- made ``ArtifactRef`` raise ``ValidationError``. That
    exception is not in ``main.py``'s status table, so the upload answered
    **500**: a server fault reported for a correct request.

    The surrounding whitespace matters for the same reason: ``text/plain ;
    charset=utf-8`` is also legal, and splitting on ``;`` alone leaves a
    trailing space inside the value.
    """

    server = _FakeSandboxServer(stdout="ok\n")
    world, _ = _world_that_wrote(server, name="notes.md")

    async def scenario(client: httpx.AsyncClient) -> tuple[int, str]:
        session_id = await _session_with_file(client)
        stored = await client.put(
            f"{code_route.CODE_PREFIX}/sessions/{session_id}/workspace/upper.md",
            headers={**HEADERS, "content-type": "TEXT/PLAIN ; charset=utf-8"},
            content=b"hello\n",
        )
        listed = [
            entry
            for entry in stored.json().get("files", [])
            if entry["name"] == "upper.md"
        ]
        return stored.status_code, listed[0]["media_type"] if listed else ""

    status, media_type = _run(world, scenario)
    assert status == 200
    # Normalised, not merely accepted: what is stored is what every later
    # decision reads, and the console routes a viewer off this exact string.
    assert media_type == "text/plain"


def test_a_name_the_workspace_does_not_bind_cannot_be_run() -> None:
    server = _FakeSandboxServer()
    world, _ = _world_that_wrote(server)

    async def scenario(client: httpx.AsyncClient) -> tuple[int, int]:
        session_id = await _session_with_file(client)
        refused = await client.post(
            _ran(world, session_id, "absent.py"), headers=HEADERS
        )
        return refused.status_code, len(server.calls)

    assert _run(world, scenario) == (404, 0)


def test_a_sandbox_that_refused_is_a_conflict_carrying_its_own_words() -> None:
    """Not a 500. Nothing here is broken; the other side declined."""

    server = _FakeSandboxServer(is_error=True, message="no container runtime")
    world, _ = _world_that_wrote(server)

    async def scenario(client: httpx.AsyncClient) -> tuple[int, str]:
        session_id = await _session_with_file(client)
        refused = await client.post(_ran(world, session_id, "sq.py"), headers=HEADERS)
        return refused.status_code, cast(str, refused.json()["detail"])

    status, detail = _run(world, scenario)

    assert status == 409
    assert "no container runtime" in detail


def test_a_script_that_failed_is_a_200_carrying_its_traceback() -> None:
    """The reader clicked to find out. A non-zero exit is the answer, not an error."""

    server = _FakeSandboxServer(
        exit_code=1, stderr='Traceback...\nNameError: name "x" is not defined\n'
    )
    world, _ = _world_that_wrote(server)

    async def scenario(client: httpx.AsyncClient) -> tuple[int, Any]:
        session_id = await _session_with_file(client)
        answered = await client.post(_ran(world, session_id, "sq.py"), headers=HEADERS)
        return answered.status_code, answered.json()

    status, body = _run(world, scenario)

    assert status == 200
    assert body["exit_code"] == 1
    assert "NameError" in body["stderr"]


def test_a_chat_session_has_nothing_to_run() -> None:
    server = _FakeSandboxServer()
    world, _ = _world_that_wrote(server)

    async def scenario(client: httpx.AsyncClient) -> int:
        await world.conversations.create_session(
            session_id="ses_chat_1", tenant_id=TENANT, owner_id=OWNER
        )
        refused = await client.post(_ran(world, "ses_chat_1", "sq.py"), headers=HEADERS)
        return refused.status_code

    assert _run(world, scenario) == 404


def test_the_whole_working_set_goes_in_beside_the_file() -> None:
    """A script that reads `data.csv` next to itself is the ordinary case."""

    server = _FakeSandboxServer(stdout="ok\n")
    world, writer = _world_that_wrote(server)

    async def scenario(client: httpx.AsyncClient) -> list[str]:
        session_id = await _session_with_file(client)
        await client.put(
            f"{code_route.CODE_PREFIX}/sessions/{session_id}/workspace/data.csv",
            headers={**HEADERS, "content-type": "text/csv"},
            content=b"a,b\n1,2\n",
        )
        await client.post(_ran(world, session_id, "sq.py"), headers=HEADERS)
        return [entry["name"] for entry in server.calls[0]["inputs"]]

    names = _run(world, scenario)

    # The file first, so a working set at the ceiling still runs the thing that
    # was clicked.
    assert names[0] == "sq.py"
    assert sorted(names) == ["data.csv", "sq.py"]
    assert writer.name == "sq.py"
