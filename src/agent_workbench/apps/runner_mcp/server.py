"""Official MCP v2 server surface for the command runner (ADR-0115)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from mcp import types
from mcp.server import Server, ServerRequestContext
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent_workbench.adapters.filesystem.commands import (
    MAX_CAPTURE_BYTES,
    RUN_TIMEOUT_SECONDS,
    LocalCommandRunner,
)
from agent_workbench.apps.runner_mcp.contract import (
    RUN_COMMAND_INPUT_SCHEMA,
    RUN_COMMAND_OUTPUT_SCHEMA,
    RunnerInputError,
    parse_run_request,
)
from agent_workbench.domain.runner import RUNNER_REMOTE_TOOL
from agent_workbench.ports.commands import CommandOutcome, CommandRunner

SERVER_NAME: Final[str] = "agent-workbench-runner"
SERVER_VERSION: Final[str] = "1.0.0"
TOOL_NAME: Final[str] = RUNNER_REMOTE_TOOL

MCP_PATH: Final[str] = "/mcp"
HEALTH_PATH: Final[str] = "/health"

#: The largest legal request is a 4,000-character command and a 1,024-character
#: path; the largest legal *response* is a megabyte of output inside a JSON-RPC
#: frame, and the SDK's body ceiling is about requests. 64 KiB leaves the
#: request room it will never need and refuses anything that is not a request.
MAX_MCP_REQUEST_BYTES: Final[int] = 64 * 1024

TOOL_DESCRIPTION: Final[str] = (
    "Run one shell command in a directory under this server's projects root "
    "and get back its exit code and what it printed, both streams interleaved. "
    f"The command is killed after {int(RUN_TIMEOUT_SECONDS)} seconds or after "
    f"{MAX_CAPTURE_BYTES} bytes of output, and says so. Stdin is closed."
)

_TOOL: Final[types.Tool] = types.Tool(
    name=TOOL_NAME,
    title="Run a shell command in a project directory",
    description=TOOL_DESCRIPTION,
    input_schema=RUN_COMMAND_INPUT_SCHEMA,
    output_schema=RUN_COMMAND_OUTPUT_SCHEMA,
    annotations=types.ToolAnnotations(
        title="Run a shell command in a project directory",
        # It runs arbitrary commands over the user's real files. The tool in
        # front of it (`project_run`) declares `destructive` and stops for a
        # person on every call; these hints say the same thing to a generic
        # client that reads annotations instead.
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)

LifespanState = dict[str, Any]


def create_server(
    *, projects_root: Path, runner: CommandRunner | None = None
) -> Server[LifespanState]:
    """Build an independent server suitable for HTTP or in-memory tests.

    ``runner`` is injectable so the protocol can be tested without spawning a
    shell; the default is the same `LocalCommandRunner` the native
    ``project_run`` uses, handed this process's own environment -- which, in
    the container this is built for, holds nothing worth scrubbing (see
    ``compose.yaml``'s `runner` service), and is scrubbed anyway.
    """

    executor = runner if runner is not None else _default_runner()

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
        del context
        if params.name != TOOL_NAME:
            return _error_result("unknown tool")
        try:
            request = parse_run_request(
                params.arguments or {}, projects_root=projects_root
            )
        except RunnerInputError as error:
            return _error_result(f"invalid run request: {error}")
        try:
            outcome = await executor.run(
                request.command,
                cwd=str(request.cwd),
                timeout_seconds=request.timeout_seconds,
            )
        except Exception:
            return _error_result("runner_failed: the command could not be started")
        return types.CallToolResult(
            content=[types.TextContent(text=_summary(outcome))],
            structured_content=_structured(outcome),
        )

    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        title="Agent Workbench command runner",
        description=(
            "A project-owned MCP server that runs one shell command per call in "
            "a directory under its projects root, and nothing else."
        ),
        instructions=TOOL_DESCRIPTION,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def create_app(
    *,
    host: str = "127.0.0.1",
    projects_root: Path,
    runner: CommandRunner | None = None,
) -> Starlette:
    """The stateless Streamable HTTP app plus a readiness route.

    ``/health`` names the tool and the root, which is what the launcher's
    smoke probe reads before it decides that `project_run` is on for this
    start (``docker/run-api-local.sh``). It answers 503 while the root is not
    a directory, because a runner whose mount did not arrive would otherwise
    advertise a tool every call of which fails on `cwd`.
    """

    async def health(request: Request) -> JSONResponse:
        del request
        mounted = projects_root.is_dir()
        return JSONResponse(
            {
                "status": "ok" if mounted else "degraded",
                "service": SERVER_NAME,
                "transport": "streamable-http",
                "tools": [TOOL_NAME],
                "projects_root": str(projects_root),
                "projects_root_mounted": mounted,
            },
            status_code=200 if mounted else 503,
        )

    return create_server(
        projects_root=projects_root, runner=runner
    ).streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=False,
        stateless_http=True,
        max_request_body_size=MAX_MCP_REQUEST_BYTES,
        host=host,
        custom_starlette_routes=[Route(HEALTH_PATH, endpoint=health, methods=["GET"])],
    )


def _default_runner() -> CommandRunner:
    # Imported here rather than at module top so that the *tests* of this
    # server, which inject a runner, do not import the bootstrap package for a
    # function they never call.
    from agent_workbench.bootstrap.child_environment import command_environment

    return LocalCommandRunner(environment=command_environment())


def _structured(outcome: CommandOutcome) -> dict[str, Any]:
    return {
        "exit_code": outcome.exit_code,
        "output": outcome.output,
        "timed_out": outcome.timed_out,
        "overflowed": outcome.overflowed,
    }


def _summary(outcome: CommandOutcome) -> str:
    if outcome.timed_out:
        return "the command was killed at the wall clock"
    if outcome.overflowed:
        return f"the command was killed after more than {MAX_CAPTURE_BYTES} bytes"
    return f"exit code: {outcome.exit_code}"


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
    "SERVER_VERSION",
    "TOOL_DESCRIPTION",
    "TOOL_NAME",
    "create_app",
    "create_server",
]
