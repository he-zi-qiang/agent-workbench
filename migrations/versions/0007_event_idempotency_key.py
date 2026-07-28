"""add stream-local durable event idempotency keys

Revision ID: 0007_event_idempotency_key
Revises: 0006_event_schema_version
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_event_idempotency_key"
down_revision: str | None = "0006_event_schema_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column("event_key", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "uq_events_stream_event_key",
        "events",
        ["stream_id", "event_key"],
        unique=True,
        postgresql_where=sa.text("event_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_events_stream_event_key", table_name="events")
    op.drop_column("events", "event_key")
