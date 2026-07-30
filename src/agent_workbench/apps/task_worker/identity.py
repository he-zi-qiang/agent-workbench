"""Restore the submitter identity at the durable Task process boundary.

The API identity adapter decides who submitted a Task and the registry stores
that immutable identity snapshot. A Worker does not infer identity from an
objective, artifact, environment variable, or model output; it only restores
the trusted registry fields into the domain value consumed by policy checks.
"""

from __future__ import annotations

from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.ports.task_registry import TaskRun


def restore_submitted_principal(task: TaskRun) -> PrincipalContext:
    """Rehydrate the immutable submitter identity recorded by the API."""

    return PrincipalContext(
        tenant_id=task.tenant_id,
        principal_id=task.owner_id,
        scopes=task.submitted_principal_scopes,
    )


__all__ = ["restore_submitted_principal"]
