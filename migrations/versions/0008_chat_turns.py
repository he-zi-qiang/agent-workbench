"""add the durable, idempotent chat-turn ledger

Revision ID: 0008_chat_turns
Revises: 0007_event_idempotency_key
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_chat_turns"
down_revision: str | None = "0007_event_idempotency_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_turns",
        sa.Column("turn_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("user_message_id", sa.String(length=128), nullable=False),
        sa.Column("assistant_message_id", sa.String(length=128), nullable=True),
        sa.Column(
            "result",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "failure_outcome",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN "
            "('running', 'release_pending', 'committed', 'withheld', "
            "'failed', 'cancelled')",
            name=op.f("ck_chat_turns_chat_turns_status"),
        ),
        sa.CheckConstraint(
            "("
            "status = 'running' AND assistant_message_id IS NULL "
            "AND result IS NULL AND failure_outcome IS NULL"
            ") OR ("
            "status = 'release_pending' AND assistant_message_id IS NULL "
            "AND result IS NOT NULL AND failure_outcome IS NULL"
            ") OR ("
            "status IN ('committed', 'withheld') "
            "AND assistant_message_id IS NOT NULL "
            "AND result IS NOT NULL AND failure_outcome IS NULL"
            ") OR ("
            "status IN ('failed', 'cancelled') AND assistant_message_id IS NULL "
            "AND result IS NULL AND failure_outcome IS NOT NULL"
            ")",
            name=op.f("ck_chat_turns_chat_turns_lifecycle"),
        ),
        sa.ForeignKeyConstraint(
            ["assistant_message_id"],
            ["messages.message_id"],
            name=op.f("fk_chat_turns_assistant_message_id"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["conversation_sessions.session_id"],
            name=op.f("fk_chat_turns_session_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_message_id"],
            ["messages.message_id"],
            name=op.f("fk_chat_turns_user_message_id"),
        ),
        sa.PrimaryKeyConstraint("turn_id", name=op.f("pk_chat_turns")),
        sa.UniqueConstraint(
            "run_id",
            name=op.f("uq_chat_turns_run_id"),
        ),
        sa.UniqueConstraint(
            "session_id",
            "idempotency_key",
            name=op.f("uq_chat_turns_session_id_idempotency_key"),
        ),
    )
    op.create_index(
        "uq_chat_turns_active_session",
        "chat_turns",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('running', 'release_pending')"),
    )


def downgrade() -> None:
    op.drop_index("uq_chat_turns_active_session", table_name="chat_turns")
    op.drop_table("chat_turns")
