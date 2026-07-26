"""Reaching the dependencies a route needs, without rebuilding them.

They are assembled once at startup and parked on the application. A route asks
for them here rather than constructing an engine or a store of its own, which
is what keeps "which database does this endpoint talk to" a question with one
answer.
"""

from __future__ import annotations

from typing import cast

from fastapi import Request

from agent_workbench.apps.api.dependencies import ApiDependencies

STATE_ATTRIBUTE = "agent_workbench_dependencies"


def dependencies_of(request: Request) -> ApiDependencies:
    return cast(ApiDependencies, getattr(request.app.state, STATE_ATTRIBUTE))


__all__ = ["STATE_ATTRIBUTE", "dependencies_of"]
