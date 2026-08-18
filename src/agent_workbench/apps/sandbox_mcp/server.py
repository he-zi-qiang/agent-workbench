"""Official MCP v2 server surface for the ephemeral Python sandbox."""

from __future__ import annotations

import base64
import json
from typing import Any, Final

from mcp import types
from mcp.server import Server, ServerRequestContext
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent_workbench.apps.sandbox_mcp.contract import (
    RUN_PYTHON_INPUT_SCHEMA,
    RUN_PYTHON_OUTPUT_SCHEMA,
    SandboxInputError,
    parse_run_request,
)
from agent_workbench.apps.sandbox_mcp.executor import (
    MAX_ENVELOPE_BYTES,
    WALL_CLOCK_SECONDS,
    SandboxExecutionError,
    SandboxExecutor,
    SandboxOutcome,
)

SERVER_NAME: Final[str] = "agent-workbench-sandbox"
SERVER_VERSION: Final[str] = "1.0.0"
TOOL_NAME: Final[str] = "run_python"

#: How a streamed line says it came from the script's stderr rather than its
#: stdout (ADR-069). Read back by `adapters/tools/sandbox.py`; deliberately a
#: readable prefix, so a generic MCP client showing progress messages verbatim
#: still shows something a person can act on.
STDERR_PREFIX: Final[str] = "stderr: "
MCP_PATH: Final[str] = "/mcp"
HEALTH_PATH: Final[str] = "/health"

#: Large enough for the biggest request the contract admits, plus the JSON-RPC
#: frame around it. Smaller than this and a legal call would be rejected by the
#: transport instead of by the schema, which is a much harder failure to read.
MAX_MCP_REQUEST_BYTES: Final[int] = MAX_ENVELOPE_BYTES

TOOL_DESCRIPTION: Final[str] = (
    "Run a Python 3 script over a set of files and get back what it printed "
    "and what it wrote. Use this whenever a step is better computed than "
    "reasoned about -- parsing, aggregating, converting, or checking a file "
    "you produced. The script runs in a throwaway container with no network "
    "access, no access to this system, and nothing left over from any earlier "
    "call, so it cannot fetch a URL or call an API. Input files appear in the "
    "working directory under the names you give them; files the script writes "
    f"there are returned. It is stopped after {WALL_CLOCK_SECONDS} seconds."
)

_TOOL: Final[types.Tool] = types.Tool(
    name=TOOL_NAME,
    title="Run Python in a sandbox",
    description=TOOL_DESCRIPTION,
    input_schema=RUN_PYTHON_INPUT_SCHEMA,
    output_schema=RUN_PYTHON_OUTPUT_SCHEMA,
    annotations=types.ToolAnnotations(
        title="Run Python in a sandbox",
        # It executes code, so it is not read-only. It is still non-destructive
        # and safe to repeat: a fresh network-less container with no history
        # cannot change anything outside the result it returns (ADR-029 §3.4).
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)

#: What the SDK's default lifespan yields. `Server[None]` does not type-check
#: against it, and this server has no lifespan state of its own to declare.
LifespanState = dict[str, Any]


def create_server(executor: SandboxExecutor | None = None) -> Server[LifespanState]:
    """Build an independent server suitable for HTTP or in-memory tests."""

    sandbox = executor if executor is not None else SandboxExecutor()

    async def list_tools(
        context: ServerRequestContext[LifespanState],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        del context, params
        return types.ListToolsResult(tools=[_TOOL])

    async def call_tool(
        context: ServerRequestContext[LifespanState],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        if params.name != TOOL_NAME:
            return _error_result("unknown tool")
        try:
            request = parse_run_request(params.arguments or {})
        except SandboxInputError as error:
            return _error_result(f"invalid sandbox request: {error}")

        streamed = 0

        async def on_output(channel: str, text: str) -> None:
            """Forward one slice of the script's output as `notifications/progress`.

            `report_progress` is a no-op when the caller did not send a
            progress token, so a client that never asked pays for a counter
            increment and nothing else.

            `progress` counts characters streamed rather than a fraction of the
            work: the protocol requires the value to increase, and this process
            genuinely does not know how far along the script is -- it is a
            container running arbitrary code that reports nothing until it
            exits. `total` stays `None` for the same reason, which is the
            protocol's own way of saying the end is unknown.

            The channel is carried as a human-readable prefix rather than a
            structured field, because a progress notification has no structured
            field to carry it. A plain line keeps this useful to any MCP client
            that renders progress messages, which a JSON blob in `message`
            would not.
            """

            nonlocal streamed
            streamed += len(text)
            body = text if channel == "stdout" else f"{STDERR_PREFIX}{text}"
            await context.session.report_progress(float(streamed), None, body)

        try:
            outcome = await sandbox.run(request, on_output=on_output)
        except SandboxExecutionError as error:
            # A refused run is an error result, not a raised exception: the
            # caller has to be able to tell a ceiling from a broken sandbox,
            # and the code is the only thing that says which.
            return _error_result(f"{error.code}: {error}")
        except Exception:
            # Third-party exceptions may carry script text or host details.
            # Keep both out of protocol results and operator-visible events.
            return _error_result("sandbox_failed: the sandbox run could not complete")

        structured = _structured(outcome)
        return types.CallToolResult(
            content=[types.TextContent(text=_summary(outcome))],
            structured_content=structured,
        )

    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        title="Agent Workbench Python sandbox",
        description=(
            "A project-owned MCP server that runs one Python script per call "
            "in a throwaway, network-less container. Files in, files out."
        ),
        instructions=TOOL_DESCRIPTION,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def create_app(
    *,
    host: str = "127.0.0.1",
    executor: SandboxExecutor | None = None,
) -> Starlette:
    """Build the stateless Streamable HTTP app plus a readiness route."""

    sandbox = executor if executor is not None else SandboxExecutor()

    async def health(request: Request) -> JSONResponse:
        del request
        # Reports the runtime, because this process is useless without one and
        # a liveness check that says "ok" regardless is what lets that go
        # unnoticed until the first call (ADR-029 §3.6).
        available = await sandbox.probe()
        return JSONResponse(
            {
                "status": "ok" if available else "degraded",
                "service": SERVER_NAME,
                "transport": "streamable-http",
                "container_runtime": sandbox.runtime,
                "container_runtime_available": available,
            },
            status_code=200 if available else 503,
        )

    return create_server(sandbox).streamable_http_app(
        streamable_http_path=MCP_PATH,
        # SSE, not a single JSON body, and this is the line that makes the
        # streaming above real rather than theoretical. Under
        # `json_response=True` the server answers one call with one JSON
        # document, and a `notifications/progress` raised while the tool is
        # still running has nowhere to go -- measured: the client's progress
        # callback fires zero times, with no error anywhere to say so
        # (ADR-069 §3).
        #
        # The cost is that a response is now a stream, so an intermediary that
        # buffers whole responses defeats the point. This server is bound to
        # loopback and reached by one process, which is the deployment where
        # that is not a risk.
        json_response=False,
        stateless_http=True,
        max_request_body_size=MAX_MCP_REQUEST_BYTES,
        host=host,
        custom_starlette_routes=[Route(HEALTH_PATH, endpoint=health, methods=["GET"])],
    )


def _structured(outcome: SandboxOutcome) -> dict[str, Any]:
    return {
        "exit_code": outcome.exit_code,
        "stdout": outcome.stdout,
        "stderr": outcome.stderr,
        "outputs": [
            {
                "name": file.name,
                "content_base64": base64.b64encode(file.content).decode("ascii"),
                "size_bytes": len(file.content),
            }
            for file in outcome.outputs
        ],
    }


def _summary(outcome: SandboxOutcome) -> str:
    """The model-facing projection: what happened, not the bytes.

    The output files are in ``structured_content`` and can be megabytes. A
    caller that wants them reads them from there; a model that is shown this
    result should be told they exist, not handed them base64-encoded.
    """

    names = ", ".join(file.name for file in outcome.outputs) or "none"
    return json.dumps(
        {
            "exit_code": outcome.exit_code,
            "stdout": outcome.stdout,
            "stderr": outcome.stderr,
            "output_files": names,
        },
        ensure_ascii=False,
    )


def _error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(text=message)],
        is_error=True,
    )


__all__ = [
    "HEALTH_PATH",
    "MAX_MCP_REQUEST_BYTES",
    "MCP_PATH",
    "SERVER_NAME",
    "TOOL_DESCRIPTION",
    "TOOL_NAME",
    "create_app",
    "create_server",
]
