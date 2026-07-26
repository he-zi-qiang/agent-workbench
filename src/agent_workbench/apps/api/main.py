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
from collections.abc import Callable, Mapping, Sequence

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response
from starlette.types import ASGIApp

from agent_workbench.application.uploads import UploadVerificationError
from agent_workbench.apps.api.dependencies import ApiDependencies, build_dependencies
from agent_workbench.apps.api.identity import UnauthenticatedError
from agent_workbench.apps.api.middleware import ControlPlaneLimit
from agent_workbench.apps.api.routes import artifacts, health, uploads
from agent_workbench.apps.api.state import STATE_ATTRIBUTE
from agent_workbench.bootstrap import load_settings
from agent_workbench.bootstrap.projections import ApiRuntimeConfig, project_api
from agent_workbench.domain.errors import NotFoundError, OutputTooLargeError

API_TITLE = "Agent Workbench"

# One table, so a later route cannot be the exception. A not-found is a 404
# whether it came from a store, a tenant mismatch or a guessed id, because
# those must not be distinguishable from outside.
ERROR_STATUS: Mapping[type[Exception], int] = {
    NotFoundError: 404,
    UnauthenticatedError: 401,
    UploadVerificationError: 409,
    OutputTooLargeError: 413,
}


def _render_error(status_code: int) -> Callable[[Request, Exception], Response]:
    def handler(request: Request, exc: Exception) -> Response:
        return JSONResponse(status_code=status_code, content={"detail": str(exc)})

    return handler


def create_app(dependencies: ApiDependencies) -> ASGIApp:
    """Build the ASGI application around already-assembled dependencies."""

    app = FastAPI(title=API_TITLE, version="0.1.0")
    setattr(app.state, STATE_ATTRIBUTE, dependencies)

    app.include_router(health.router)
    app.include_router(uploads.router)
    app.include_router(artifacts.router)

    for failure, status_code in ERROR_STATUS.items():
        app.add_exception_handler(failure, _render_error(status_code))

    # The data plane is exempt: capping a document transfer at the control
    # limit is the same as not accepting documents.
    return ControlPlaneLimit(
        app,
        max_bytes=dependencies.max_control_request_body_bytes,
        is_exempt=uploads.is_data_plane_path,
    )


def build_app(config: ApiRuntimeConfig) -> tuple[ASGIApp, ApiDependencies]:
    """Assemble dependencies and the application they serve."""

    dependencies = build_dependencies(config)
    return create_app(dependencies), dependencies


def main(argv: Sequence[str] | None = None) -> int:
    """Serve the API with the configured host, port and shutdown grace."""

    parser = argparse.ArgumentParser(
        prog="agent-api",
        description="Run the Agent Workbench HTTP control plane.",
    )
    parser.parse_args(argv)

    import uvicorn

    config = project_api(load_settings())
    app, _ = build_app(config)
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
