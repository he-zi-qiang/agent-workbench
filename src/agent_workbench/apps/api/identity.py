"""Resolving who is calling, at the edge where that is knowable.

Identity is an interface-layer result, never a request body field. A caller
that could name its own owner or tenant could name someone else's, so the
principal is built here from the transport's own authenticated material and
handed to application services already resolved.

What is here is not authentication. It reads two headers, which is enough to
exercise tenant scoping end to end and is exactly what a deployment must never
run. The guard is the deployment scope: a process configured as ``remote``
refuses to start with this resolver, so the way to expose this service is to
implement a real one first, not to remember not to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from starlette.requests import Request

from agent_workbench.domain.errors import AgentWorkbenchError, ErrorCode
from agent_workbench.domain.policies import PrincipalContext

TENANT_HEADER = "x-tenant-id"
PRINCIPAL_HEADER = "x-principal-id"
SCOPES_HEADER = "x-principal-scopes"


class UnauthenticatedError(AgentWorkbenchError):
    """The request carried no usable identity."""

    code: ClassVar[ErrorCode] = "policy_denied"


@dataclass(frozen=True, slots=True)
class HeaderPrincipalResolver:
    """Development-only identity, taken from request headers."""

    def resolve(self, request: Request) -> PrincipalContext:
        tenant_id = request.headers.get(TENANT_HEADER, "").strip()
        principal_id = request.headers.get(PRINCIPAL_HEADER, "").strip()
        if not tenant_id or not principal_id:
            raise UnauthenticatedError(
                f"{TENANT_HEADER} and {PRINCIPAL_HEADER} are required"
            )

        raw_scopes = request.headers.get(SCOPES_HEADER, "")
        scopes = tuple(
            scope.strip() for scope in raw_scopes.split(",") if scope.strip()
        )
        return PrincipalContext(
            principal_id=principal_id,
            tenant_id=tenant_id,
            scopes=scopes,
        )


__all__ = [
    "PRINCIPAL_HEADER",
    "SCOPES_HEADER",
    "TENANT_HEADER",
    "HeaderPrincipalResolver",
    "UnauthenticatedError",
]
