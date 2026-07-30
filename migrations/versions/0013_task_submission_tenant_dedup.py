"""scope Task submission idempotency to a tenant and owner

Revision ID: 0013_task_tenant_dedup
Revises: 0012_task_submitted_semantics

``owner_id`` is not globally unique.  The original constraint accidentally
made one tenant's idempotency key reserve the same key for every tenant with a
matching principal id.  Keep the existing rows and replace only the unique
constraint; no Task semantics change in this migration.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# Alembic's stock `alembic_version.version_num` is VARCHAR(32). Keep this
# identifier below that limit: a longer Python filename is harmless, but an
# overlong revision id makes an otherwise successful migration fail while
# writing its version marker.
revision: str = "0013_task_tenant_dedup"
down_revision: str | None = "0012_task_submitted_semantics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("uq_task_runs_owner_id_submission_dedup_key"),
        "task_runs",
        type_="unique",
    )
    op.create_unique_constraint(
        op.f("uq_task_runs_tenant_id_owner_id_submission_dedup_key"),
        "task_runs",
        ["tenant_id", "owner_id", "submission_dedup_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("uq_task_runs_tenant_id_owner_id_submission_dedup_key"),
        "task_runs",
        type_="unique",
    )
    op.create_unique_constraint(
        op.f("uq_task_runs_owner_id_submission_dedup_key"),
        "task_runs",
        ["owner_id", "submission_dedup_key"],
    )
