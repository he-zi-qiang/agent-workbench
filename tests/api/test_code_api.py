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
import os
import tomllib
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
from agent_workbench.application.code_approvals import (
    ApprovalNotPendingError,
    ApprovalScope,
    CodeApprovalRegistry,
    StandingApprovalRefusedError,
)
from agent_workbench.application.code_session import (
    CodeCapacityError,
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
from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.events import UngroundedAnswerCommitted
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.domain.runs import AgentOutcome, RunBudget
from agent_workbench.ports.event_log import EventScope

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
            CodeTurnBusyError,
            CodeCapacityError,
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


def _assembled_settings(root: Path, *, code_enabled: bool) -> Settings:
    with DEFAULT_CONFIG_FILE.open("rb") as handle:
        payload: dict[str, Any] = tomllib.load(handle)
    dsn = os.environ["AGENT_WORKBENCH_TEST_DSN"]
    payload["database"].update(dsn=dsn, guard_dsn=dsn, listen_dsn=dsn)
    payload["model"]["main"]["model_id"] = "deepseek-chat"
    payload["model"]["compact"]["model_id"] = "deepseek-chat"
    payload["artifact_store"]["local_root"] = str(root)
    payload["secrets"] = {"deepseek_api_key": "sk-unit-test"}
    payload["code"] = {**payload["code"], "enabled": code_enabled}
    return Settings(**payload)


def _booted(root: Path, *, code_enabled: bool) -> tuple[bool, int, str]:
    async def execute() -> tuple[bool, int, str]:
        dependencies = build_dependencies(
            project_api(_assembled_settings(root, code_enabled=code_enabled))
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
