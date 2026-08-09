"""One Task-shaped protocol-to-Runtime round over the official MCP protocol.

The adapter unit tests can prove every mapping branch, but they cannot prove
that a discovered name survives all three authority ceilings and reaches the
existing Agent Runtime.  This test therefore uses the official SDK's in-memory
server, the real MCP discovery adapter, the real ToolGateway and a Task request
built from the writer profile.  No provider, database or listening port is
needed, so the protocol path runs in ordinary CI instead of becoming a skipped
"integration" claim.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from mcp.server import MCPServer

from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.mcp.client import connect_mcp_client
from agent_workbench.adapters.mcp.registry_source import discover_bindings
from agent_workbench.adapters.memory import InMemoryArtifactStore, InMemoryEventLog
from agent_workbench.adapters.models.fake import FakeModel, ScriptedTurn
from agent_workbench.adapters.policy import EnvelopePolicyEngine
from agent_workbench.adapters.tools import StaticToolRegistry
from agent_workbench.domain.messages import ToolResultBlock
from agent_workbench.domain.policies import AuthorizationEnvelope, PrincipalContext
from agent_workbench.domain.runs import RunBudget, TokenUsage, TraceContext
from agent_workbench.domain.tasks import TaskState
from agent_workbench.domain.tools import ToolCall
from agent_workbench.ports.cancellation import CancellationSource
from agent_workbench.ports.event_log import EventScope
from agent_workbench.runtime import ClaudeLikeAgentRuntime, ToolExecutor, ToolGateway
from agent_workbench.workflows.agent_nodes import TaskRunContext, build_request

LOCAL_NAME = "mcp_office_render_document"


def test_a_writer_calls_an_mcp_tool_through_the_task_runtime() -> None:
    async def scenario() -> None:
        calls: list[str] = []
        server = MCPServer("agent-workbench-e2e")

        async def render_document(title: str) -> str:
            calls.append(title)
            return f"rendered:{title}"

        server.add_tool(render_document, name="render-document")

        # The production adapter receives a URL. The official v2 Client also
        # accepts an in-memory MCPServer; using the same constructor gives this
        # test real server/discover/list/call protocol without a flaky test port.
        async with connect_mcp_client(cast(Any, server), timeout_seconds=5) as client:
            artifacts = InMemoryArtifactStore()
            bindings = await discover_bindings(
                alias="office",
                allowed_remote_tools=("render-document",),
                timeout_seconds=5,
                client=client,
                artifacts=artifacts,
                artifact_threshold_bytes=8,
                max_result_bytes=1_048_576,
                max_artifact_bytes=1_048_576,
            )
            assert tuple(binding.spec.name for binding in bindings) == (LOCAL_NAME,)
            assert bindings[0].spec.idempotency == "safe"
            assert bindings[0].operation_key is None

            registry = StaticToolRegistry(bindings)
            model = FakeModel(
                (
                    ScriptedTurn(
                        text="I will render the document.",
                        tool_calls=(
                            ToolCall(
                                tool_call_id="tool_call_1",
                                tool_name=LOCAL_NAME,
                                arguments={"title": "Quarterly plan"},
                            ),
                        ),
                        usage=TokenUsage(input_tokens=20, output_tokens=8),
                    ),
                    ScriptedTurn(
                        text="The document was rendered.",
                        usage=TokenUsage(input_tokens=30, output_tokens=6),
                    ),
                )
            )
            runtime = ClaudeLikeAgentRuntime(
                model=model,
                gateway=ToolGateway(
                    registry=registry,
                    policy=EnvelopePolicyEngine(registry),
                    executor=ToolExecutor(),
                ),
                policy_identity="policy-e2e:0123456789abcdef",
            )

            task_context = TaskRunContext(
                trace=TraceContext(
                    agent_run_id="run_writer_1",
                    task_id="task_mcp_1",
                    workflow_thread_id="thread_mcp_1",
                    graph_node_id="synthesize",
                ),
                stream_id="stream_mcp_1",
                principal=PrincipalContext(
                    tenant_id="tenant_a",
                    principal_id="user_1",
                    scopes=("mcp:office",),
                ),
                envelope=AuthorizationEnvelope(
                    allowed_tools=(LOCAL_NAME,),
                    max_tool_risk="external",
                    approval_required_risks=(),
                ),
                budget=RunBudget(max_steps=4, max_tool_calls=2),
            )
            request = build_request(
                "synthesize",
                TaskState(
                    task_id="task_mcp_1",
                    objective="Render the quarterly plan.",
                ),
                task_context,
                dynamic_tools={"synthesis": (LOCAL_NAME,)},
            )
            assert request.tool_names == (LOCAL_NAME,)

            log = InMemoryEventLog()
            outcome = await runtime.run(
                request,
                ScopedEventSink(
                    log,
                    EventScope(
                        stream_id="stream_mcp_1",
                        run_id="run_writer_1",
                        task_id="task_mcp_1",
                        graph_node_id="synthesize",
                    ),
                ),
                CancellationSource(),
            )

            events = await log.read("stream_mcp_1")
            event_types = [event.event_type for event in events]
            tool_events = [
                event_type
                for event_type in event_types
                if event_type
                in {
                    "ToolProposed",
                    "PermissionResolved",
                    "ToolStarted",
                    "ToolCompleted",
                }
            ]

            assert outcome.status == "completed"
            assert outcome.output_text == "The document was rendered."
            assert calls == ["Quarterly plan"]
            assert tool_events == [
                "ToolProposed",
                "PermissionResolved",
                "ToolStarted",
                "ToolCompleted",
            ]
            assert model.requests[0].tools[0].name == LOCAL_NAME
            tool_block = model.requests[1].messages[-1].content[0]
            assert isinstance(tool_block, ToolResultBlock)
            assert tool_block.artifact is not None
            assert "MCP result stored as" in tool_block.text
            assert (
                await artifacts.get(
                    tenant_id="tenant_a",
                    artifact_id=tool_block.artifact.artifact_id,
                    principal_id="user_1",
                )
                == b"rendered:Quarterly plan"
            )

    asyncio.run(scenario())
