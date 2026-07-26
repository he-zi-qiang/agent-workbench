"""Conversation sessions and their messages.

Revision ID: 0001_conversations
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_conversations"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversation_sessions",
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("session_id", name="pk_conversation_sessions"),
    )
    op.create_index(
        "ix_conversation_sessions_tenant_id_session_id",
        "conversation_sessions",
        ["tenant_id", "session_id"],
    )

    op.create_table(
        "messages",
        sa.Column("message_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["conversation_sessions.session_id"],
            name="fk_messages_session_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("message_id", name="pk_messages"),
        # Gap-free ordering is a promise of the port. The lock that assigns a
        # position makes it true; this constraint is what notices if the lock
        # is ever bypassed.
        sa.UniqueConstraint(
            "session_id",
            "sequence",
            name="uq_messages_session_id_sequence",
        ),
    )
    op.create_index(
        "ix_messages_session_id_sequence",
        "messages",
        ["session_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_index("ix_messages_session_id_sequence", table_name="messages")
    op.drop_table("messages")
    op.drop_index(
        "ix_conversation_sessions_tenant_id_session_id",
        table_name="conversation_sessions",
    )
    op.drop_table("conversation_sessions")
