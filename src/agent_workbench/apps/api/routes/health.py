"""Liveness and readiness, which answer different questions.

Liveness asks whether the process is running. It touches nothing: an endpoint
that checks a dependency reports the process dead during an outage it has no
part in, and an orchestrator restarts it for no reason.

Readiness asks whether it can serve, so it does check, under a bound. A
readiness probe with no timeout stops being a probe the moment the thing it
probes stops answering.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from agent_workbench.apps.api.state import dependencies_of

READINESS_TIMEOUT_SECONDS = 2.0

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "live"}


@router.get("/health/ready")
async def ready(request: Request, response: Response) -> dict[str, str]:
    dependencies = dependencies_of(request)
    try:
        async with asyncio.timeout(READINESS_TIMEOUT_SECONDS):
            async with dependencies.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    except Exception:
        # The reason belongs in the logs, not in a response anyone can
        # request: it describes the deployment's internals.
        response.status_code = 503
        return {"status": "unready"}
    return {"status": "ready"}


__all__ = ["READINESS_TIMEOUT_SECONDS", "router"]
