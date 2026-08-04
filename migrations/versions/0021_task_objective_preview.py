"""label task rows with the objective they were submitted for

A Task list that shows only ``task_969398ec...ad7e`` is a list nobody can read.
The objective lives in the input artifact, which the list endpoint cannot fetch
per row, so submission copies a bounded prefix here for display.

Nullable and not backfilled. The prefix is derived from the input artifact, and
reading every historical Task's artifact inside a migration would make schema
change depend on the artifact store being reachable and on every stored input
still parsing. Rows written before this column show their id instead, which is
what they already showed.

Revision ID: 0021_task_objective_preview
Revises: 0020_knowledge_bases
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_task_objective_preview"
down_revision: str | None = "0020_knowledge_bases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "task_runs",
        sa.Column("objective_preview", sa.String(length=200), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("task_runs", "objective_preview")
