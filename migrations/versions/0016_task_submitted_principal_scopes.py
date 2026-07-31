"""persist the Task submission-time principal scope ceiling

Revision ID: 0016_task_principal_scopes
Revises: 0015_task_execution_lease

Older rows have no durable scope snapshot and are backfilled to ``[]``. That
is deny-shaped: a resumed Task cannot acquire an external permission that was
not present in its original durable state. The server default is temporary and
removed before the migration completes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_task_principal_scopes"
down_revision: str | None = "0015_task_execution_lease"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "task_runs",
        sa.Column(
            "submitted_principal_scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.execute(
        "UPDATE task_runs SET submitted_principal_scopes = '[]'::jsonb "
        "WHERE submitted_principal_scopes IS NULL"
    )
    op.alter_column("task_runs", "submitted_principal_scopes", server_default=None)


def downgrade() -> None:
    op.drop_column("task_runs", "submitted_principal_scopes")
