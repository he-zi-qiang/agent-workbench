"""The one acceptance item from the local-profile checklist that needs no model.

``docs/web-mcp-local.md`` §5 lists four things a real Task run has to show. The
first three are facts in an event stream and need a provider key, PostgreSQL and
a Worker. The fourth -- that removing ``mcp:web`` from the principal makes the
Gateway refuse the same tool -- is decided entirely by the policy engine, so it
is automated here rather than left as a manual step nobody repeats.

Paired, because either half alone proves nothing: a Gateway that refused every
call satisfies the refusal, and one that never checked scopes satisfies the
control.
"""

from __future__ import annotations

import asyncio

from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.mcp.client import (
    ProgressSink,
    RemoteCallResult,
    RemoteToolDefinition,
    RemoteToolPage,
)
from agent_workbench.adapters.mcp.registry_source import discover_bindings
from agent_workbench.adapters.memory import InMemoryArtifactStore, InMemoryEventLog
from agent_workbench.adapters.policy import EnvelopePolicyEngine
from agent_workbench.adapters.tools import StaticToolRegistry
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PrincipalContext,
)
from agent_workbench.domain.schema import JsonObject
from agent_workbench.domain.tools import ToolCall, ToolResult
from agent_workbench.ports.event_log import EventScope
from agent_workbench.runtime import ToolGateway

LOCAL_NAME = "mcp_web_fetch_page"
REQUIRED_SCOPE = "mcp:web"


class _WebDirectory:
    """A directory shaped like the project's own web server's."""

    async def list_tools_page(self, cursor: str | None) -> RemoteToolPage:
        assert cursor is None
        return RemoteToolPage(
            tools=(
                RemoteToolDefinition(
                    name="fetch_page",
                    description="Read one web page and return its readable text.",
                    input_schema={
                        "type": "object",
                        "properties": {"url": {"type": "string"}},
                        "required": ["url"],
                        "additionalProperties": False,
                    },
                ),
            )
        )

    async def call_tool(
        self,
        name: str,
        arguments: JsonObject,
        *,
        on_progress: ProgressSink | None = None,
    ) -> RemoteCallResult:
        del name, arguments
        return RemoteCallResult(content=())


STREAM = "stream_" + "0" * 24


def _decide(scopes: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    """Drive the real Gateway, not the policy engine directly.

    The claim in the runbook is that the *Gateway* refuses, and the Gateway is
    what a Task actually meets. Calling the engine on its own would prove the
    rule exists without proving anything routes to it.
    """

    async def scenario() -> tuple[str, tuple[str, ...]]:
        bindings = await discover_bindings(
            alias="web",
            allowed_remote_tools=("fetch_page",),
            timeout_seconds=5,
            client=_WebDirectory(),
            artifacts=InMemoryArtifactStore(),
            artifact_threshold_bytes=4_096,
            max_result_bytes=8_192,
            max_artifact_bytes=1_048_576,
        )
        registry = StaticToolRegistry(bindings)
        assert registry.get(LOCAL_NAME) is not None
        gateway = ToolGateway(
            registry=registry,
            policy=EnvelopePolicyEngine(registry=registry),
        )
        events = InMemoryEventLog()
        sink = ScopedEventSink(
            events,
            EventScope(stream_id=STREAM, run_id="run_" + "0" * 28),
        )
        call = ToolCall(
            tool_call_id="toolu_" + "0" * 20,
            tool_name=LOCAL_NAME,
            arguments={"url": "https://research.example/doc"},
        )
        context = _context(scopes)
        prepared = await gateway.prepare(call, context=context, sink=sink)
        outcome = "allowed"
        if isinstance(prepared, ToolResult):
            outcome = _code_of(prepared)
        else:
            authorized = await gateway.authorize(prepared, context=context, sink=sink)
            if isinstance(authorized, ToolResult):
                outcome = _code_of(authorized)
        recorded = await events.read(STREAM)
        return outcome, tuple(
            str(getattr(item.payload, "reason_code", ""))
            for item in recorded
            if item.event_type == "PermissionResolved"
        )

    return asyncio.run(scenario())


def _code_of(result: ToolResult) -> str:
    assert result.error is not None
    return result.error.code


def _context(scopes: tuple[str, ...]) -> ExecutionContext:
    return ExecutionContext(
        principal=PrincipalContext(
            tenant_id="tenant_local",
            principal_id="user_local",
            scopes=scopes,
        ),
        envelope=AuthorizationEnvelope(
            allowed_tools=(LOCAL_NAME,),
            max_tool_risk="external",
            approval_required_risks=(),
        ),
        agent_run_id="run_" + "0" * 28,
        policy_identity="local-profile",
    )


def test_a_principal_holding_the_scope_reaches_the_web_tool() -> None:
    """The control. The envelope allows it and the scope is held, so the only
    thing left to refuse it would be a Gateway that refuses everything."""

    outcome, reasons = _decide((REQUIRED_SCOPE,))

    assert outcome == "allowed"
    assert reasons == ("within_submitted_envelope",)


def test_the_same_tool_is_refused_without_the_scope() -> None:
    """`docs/web-mcp-local.md` §5, item 4.

    The envelope is identical and the tool is registered; the only difference is
    the principal. A deployment that enabled the web profile still cannot let an
    unscoped caller reach the outside world.
    """

    for held in ((), ("mcp:word",)):
        outcome, reasons = _decide(held)

        assert outcome == "policy_denied"
        # The model-facing code says only "denied"; *which* scope was missing is
        # an operator detail and stays in the event stream. Both halves are
        # asserted because a refusal an operator cannot diagnose is a support
        # ticket, and a model-facing message naming an internal scope is a hint
        # to whatever is steering the model.
        assert reasons == ("missing_permission_scope",)


def test_the_derived_scope_is_the_alias_not_the_remote_name() -> None:
    """One scope per server, so revoking a deployment's reader is one grant.

    A per-tool scope would mean removing `mcp:web` still left
    `download_document` reachable, which is the opposite of what an operator
    revoking the capability means.
    """

    async def scenario() -> tuple[str, ...]:
        bindings = await discover_bindings(
            alias="web",
            allowed_remote_tools=("fetch_page",),
            timeout_seconds=5,
            client=_WebDirectory(),
            artifacts=InMemoryArtifactStore(),
            artifact_threshold_bytes=4_096,
            max_result_bytes=8_192,
            max_artifact_bytes=1_048_576,
        )
        return bindings[0].spec.permission_scopes

    assert asyncio.run(scenario()) == (REQUIRED_SCOPE,)
