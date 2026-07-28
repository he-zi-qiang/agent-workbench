"""add fixed execution leases to running chat turns

Revision ID: 0009_chat_turn_lease
Revises: 0008_chat_turns
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_chat_turn_lease"
down_revision: str | None = "0008_chat_turns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_turns",
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    # A process that was running during deployment has no trustworthy owner
    # after restart. Backfill it at the database clock's current instant so
    # the next claim/reaper closes it as stale instead of granting it a fresh
    # execution window.
    op.execute(
        sa.text(
            "UPDATE chat_turns SET lease_until = CURRENT_TIMESTAMP "
            "WHERE status = 'running'"
        )
    )
    op.create_check_constraint(
        op.f("ck_chat_turns_chat_turns_lease"),
        "chat_turns",
        "(status = 'running' AND lease_until IS NOT NULL) OR "
        "(status <> 'running' AND lease_until IS NULL)",
    )
    op.create_index(
        "ix_chat_turns_expired_running",
        "chat_turns",
        ["lease_until", "turn_id"],
        unique=False,
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("ix_chat_turns_expired_running", table_name="chat_turns")
    op.drop_constraint(
        op.f("ck_chat_turns_chat_turns_lease"),
        "chat_turns",
        type_="check",
    )
    op.drop_column("chat_turns", "lease_until")
