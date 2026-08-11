"""Restore the document owner's identity at the ingestion process boundary.

The same rule the Task Worker follows (ADR-012), for the same reason. The API
decided who owned an upload and PostgreSQL stored that decision on the
document row; this restores those trusted fields into the domain value the
graph extraction run is attributed to. Nothing here infers identity -- not from
an outbox payload, not from the content being read, not from the environment.

Scopes are deliberately empty. A second-pass extraction is a toolless model
call under a deny-shaped envelope: it reads one passage and returns JSON. Any
scope granted here would be one nothing in that path needs and everything in
it would inherit.
"""

from __future__ import annotations

from agent_workbench.domain.policies import PrincipalContext


def restore_document_owner(*, tenant_id: str, owner_id: str) -> PrincipalContext:
    """Rehydrate the owner PostgreSQL recorded for a document."""

    return PrincipalContext(tenant_id=tenant_id, principal_id=owner_id)


__all__ = ["restore_document_owner"]
