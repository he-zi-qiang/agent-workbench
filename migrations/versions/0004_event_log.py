"""durable event log with per-stream gap-free sequences

Revision ID: 0004_event_log
Revises: 0003_outbox_lease
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004_event_log"
down_revision: str | None = "0003_outbox_lease"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_streams",
        sa.Column("stream_id", sa.String(length=128), primary_key=True),
        sa.Column("last_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "events",
        sa.Column("event_id", sa.String(length=128), primary_key=True),
        sa.Column("stream_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("task_id", sa.String(length=128), nullable=True),
        sa.Column("graph_node_id", sa.String(length=128), nullable=True),
        sa.Column("parent_event_id", sa.String(length=128), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("stream_id", "sequence", name="uq_events_stream_sequence"),
    )
    op.create_index("ix_events_stream_sequence", "events", ["stream_id", "sequence"])


def downgrade() -> None:
    op.drop_index("ix_events_stream_sequence", table_name="events")
    op.drop_table("events")
    op.drop_table("event_streams")
