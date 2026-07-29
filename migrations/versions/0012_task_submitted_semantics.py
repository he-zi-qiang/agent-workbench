"""record what a Task meant when it was submitted

Revision ID: 0012_task_submitted_semantics
Revises: 0011_task_runs

The columns are NOT NULL with no default, so this refuses to run against a
table that already has rows. That is deliberate: a Task submitted before these
existed has no submitted semantics, and backfilling a placeholder would make
one up -- which is exactly what a resume would then restore.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_task_submitted_semantics"
down_revision: str | None = "0011_task_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "task_runs",
        sa.Column(
            "run_semantics_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
    )
    op.add_column(
        "task_runs",
        sa.Column("run_semantics_revision", sa.String(length=128), nullable=False),
    )
    op.add_column(
        "task_runs",
        sa.Column("submitted_policy_revision", sa.String(length=128), nullable=False),
    )
    op.add_column(
        "task_runs",
        sa.Column("submitted_policy_fingerprint", sa.String(length=64), nullable=False),
    )
    op.add_column(
        "task_runs",
        sa.Column(
            "submitted_authorization_envelope",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("task_runs", "submitted_authorization_envelope")
    op.drop_column("task_runs", "submitted_policy_fingerprint")
    op.drop_column("task_runs", "submitted_policy_revision")
    op.drop_column("task_runs", "run_semantics_revision")
    op.drop_column("task_runs", "run_semantics_snapshot")
