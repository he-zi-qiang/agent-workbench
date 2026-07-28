"""track which revision the index has already been told about

Revision ID: 0005_last_applied_revision
Revises: 0004_event_log
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_last_applied_revision"
down_revision: str | None = "0004_event_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "last_applied_revision",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("documents", "last_applied_revision")
