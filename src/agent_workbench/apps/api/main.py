"""The API application: assembled from settings, or not started at all.

Configuration is read once, here, through the one loader every process uses.
Routes receive finished objects, so there is no second place where a limit or a
DSN could be interpreted differently.

Domain failures are translated to status codes in one exception handler rather
than at each route. A not-found is a 404 whether it came from a store, a tenant
mismatch or a guessed id, because those must not be distinguishable, and
keeping the mapping in one place is what stops a later route from being the
exception.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response
from starlette.types import ASGIApp

from agent_workbench.adapters.telemetry import EventLoopLagWatchdog
from agent_workbench.application.chat import ChatExecutionError
from agent_workbench.application.tasks import TimelineUnavailableError
from agent_workbench.application.uploads import UploadVerificationError
from agent_workbench.apps.api.dependencies import ApiDependencies, build_dependencies
from agent_workbench.apps.api.identity import UnauthenticatedError
from agent_workbench.apps.api.middleware import ControlPlaneLimit
from agent_workbench.apps.api.routes import (
    approvals,
    artifacts,
    chat,
    events,
    health,
    knowledge_bases,
    search,
    tasks,
    uploads,
)
from agent_workbench.apps.api.routes.approvals import InvalidApprovalCursorError
from agent_workbench.apps.api.routes.search import SearchUnavailableError
from agent_workbench.apps.api.routes.tasks import InvalidTaskCursorError
from agent_workbench.apps.api.sse import TooManyLiveSubscribersError
from agent_workbench.apps.api.state import STATE_ATTRIBUTE
from agent_workbench.apps.api.web import mount_console, resolve_web_directory
from agent_workbench.bootstrap import load_settings
from agent_workbench.bootstrap.projections import ApiRuntimeConfig, project_api
from agent_workbench.domain.errors import NotFoundError, OutputTooLargeError
from agent_workbench.ports.approvals import ApprovalNotDecidableError
from agent_workbench.ports.conversation_store import (
    ChatTurnBusyError,
    ChatTurnConflictError,
)
from agent_workbench.ports.documents import KnowledgeBaseMismatchError
from agent_workbench.ports.task_registry import (
    TaskSubmissionConflictError,
    TaskTransitionRejectedError,
)

API_TITLE = "Agent Workbench"

# One table, so a later route cannot be the exception. A not-found is a 404
# whether it came from a store, a tenant mismatch or a guessed id, because
# those must not be distinguishable from outside.
ERROR_STATUS: Mapping[type[Exception], int] = {
    NotFoundError: 404,
    UnauthenticatedError: 401,
    UploadVerificationError: 409,
    KnowledgeBaseMismatchError: 409,
    ChatTurnBusyError: 409,
    ChatTurnConflictError: 409,
    OutputTooLargeError: 413,
    TaskTransitionRejectedError: 409,
    # The Task moved while a human was thinking -- cancelled, most often.
    # A conflict rather than a 404: the caller was allowed to see this
    # approval, so hiding it now would be a different lie.
    ApprovalNotDecidableError: 409,
    InvalidTaskCursorError: 400,
    InvalidApprovalCursorError: 400,
    TimelineUnavailableError: 409,
    SearchUnavailableError: 409,
    # Not 503: the process is healthy and this stream is servable, there are
    # just already as many live subscribers on it as it may have. A client that
    # closed a tab and reopened it should retry, which is what 429 asks for.
    TooManyLiveSubscribersError: 429,
}


def _render_error(status_code: int) -> Callable[[Request, Exception], Response]:
    def handler(request: Request, exc: Exception) -> Response:
        return JSONResponse(status_code=status_code, content={"detail": str(exc)})

    return handler


def _render_chat_execution_error(request: Request, exc: Exception) -> Response:
    """Expose a terminal run as a terminal run, never as an empty answer."""

    if not isinstance(exc, ChatExecutionError):  # pragma: no cover - registered type
        raise exc
    outcome = exc.outcome
    if outcome.status == "cancelled":
        status_code = 409
    elif outcome.stop_reason == "deadline":
        status_code = 504
    else:
        status_code = 502
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": str(exc),
            "run_id": outcome.agent_run_id,
            "status": outcome.status,
            "stop_reason": outcome.stop_reason,
            "error": (
                outcome.error.model_dump(mode="json")
                if outcome.error is not None
                else None
            ),
        },
    )


def _render_task_submission_conflict(request: Request, exc: Exception) -> Response:
    """Report a duplicate-key conflict without reflecting owner metadata."""

    if not isinstance(exc, TaskSubmissionConflictError):  # pragma: no cover
        raise exc
    return JSONResponse(
        status_code=409,
        content={"detail": "task submission conflicts with the idempotency key"},
    )


def create_app(
    dependencies: ApiDependencies, *, web_directory: Path | None = None
) -> ASGIApp:
    """Build the ASGI application around already-assembled dependencies."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        background_tasks: tuple[asyncio.Task[object], ...] = ()
        try:
            # This is intentionally async lifespan work, rather than a sync
            # dependency factory trying to drive an event loop. A failed check
            # owns no serving routes and is followed by disposal below.
            await dependencies.startup()
            background_tasks = tuple(
                asyncio.create_task(worker.run_forever(), name=name)
                for worker, name in (
                    (dependencies.chat_reaper, "chat-turn-reaper"),
                    (
                        dependencies.chat_pending_recovery,
                        "chat-pending-release-recovery",
                    ),
                    # Unconditional, unlike the two above: a process serving no
                    # chat still embeds, parses and uploads, and those are the
                    # calls that block the loop for everything else on it.
                    (
                        EventLoopLagWatchdog(
                            telemetry=dependencies.telemetry.telemetry
                        ),
                        "event-loop-lag-watchdog",
                    ),
                )
                if worker is not None
            )
            yield
        finally:
            for task in background_tasks:
                task.cancel()
            if background_tasks:
                await asyncio.gather(*background_tasks, return_exceptions=True)
            await dependencies.dispose()

    app = FastAPI(title=API_TITLE, version="0.1.0", lifespan=lifespan)
    setattr(app.state, STATE_ATTRIBUTE, dependencies)

    app.include_router(health.router)
    app.include_router(uploads.router)
    app.include_router(artifacts.router)
    app.include_router(knowledge_bases.router)
    app.include_router(tasks.router)
    app.include_router(approvals.router)
    if dependencies.serves_search:
        # Mounted without a model. Retrieval is the half of chat that needs
        # no provider, and a deployment that has indexed documents should be
        # able to look at them.
        app.include_router(search.router)
    if dependencies.serves_chat:
        # Mounted only when the process can answer. A route that 500s per
        # request is a worse answer than a 404 a client detects once.
        app.include_router(chat.router)
        # Subscribing is only meaningful where there are turns to subscribe to.
        app.include_router(events.router)

    if web_directory is not None:
        # Mounted after every router, so an API path is never answered by a
        # static file. Starlette matches in registration order.
        mount_console(app, web_directory)

    for failure, status_code in ERROR_STATUS.items():
        app.add_exception_handler(failure, _render_error(status_code))
    app.add_exception_handler(ChatExecutionError, _render_chat_execution_error)
    app.add_exception_handler(
        TaskSubmissionConflictError,
        _render_task_submission_conflict,
    )

    # The data plane is exempt: capping a document transfer at the control
    # limit is the same as not accepting documents.
    return ControlPlaneLimit(
        app,
        max_bytes=dependencies.max_control_request_body_bytes,
        is_exempt=uploads.is_data_plane_path,
    )


def build_app(
    config: ApiRuntimeConfig,
    *,
    with_chat: bool = True,
    web_directory: Path | None = None,
) -> tuple[ASGIApp, ApiDependencies]:
    """Assemble dependencies and the application they serve.

    ``with_chat`` is threaded through rather than hidden because assembling
    chat loads the embedding model. Eager is right for a server; paying it in
    something that only exercises uploads is not.
    """

    dependencies = build_dependencies(config, with_chat=with_chat)
    return create_app(dependencies, web_directory=web_directory), dependencies


def main(argv: Sequence[str] | None = None) -> int:
    """Serve the API with the configured host, port and shutdown grace."""

    parser = argparse.ArgumentParser(
        prog="agent-api",
        description="Run the Agent Workbench HTTP control plane.",
    )
    parser.add_argument(
        "--without-chat",
        action="store_true",
        help=(
            "Serve uploads, artifacts, tasks and approvals without assembling "
            "chat. Use when this deployment has no model provider: the chat "
            "route is not registered at all, rather than registered and failing "
            "every request."
        ),
    )
    parser.add_argument(
        "--web-dir",
        help=(
            "Serve the browser console from this directory, under /ui, on the "
            "same origin as the API. Omitted, no console is mounted at all: a "
            "page that loads and then fails every request is worse than one "
            "404. The directory must exist and contain index.html, checked at "
            "startup rather than on the first request."
        ),
    )
    arguments = parser.parse_args(argv)

    import uvicorn

    config = project_api(load_settings())
    # Not a degraded mode that hides a misconfiguration. `build_model` refuses to
    # start a process whose model it could not call, and that refusal is correct
    # -- a process that answers nothing while passing its health check turns a
    # configuration mistake into an incident with a long path back to its cause.
    # This flag is the other honest answer to the same situation: say up front
    # that chat is not served here, and let uploads and Tasks run.
    app, _ = build_app(
        config,
        with_chat=not arguments.without_chat,
        web_directory=(
            None
            if arguments.web_dir is None
            else resolve_web_directory(arguments.web_dir)
        ),
    )
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        timeout_graceful_shutdown=config.shutdown_grace_seconds,
        log_level=config.log_level.lower(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
