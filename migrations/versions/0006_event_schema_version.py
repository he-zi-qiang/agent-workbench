"""persist the event envelope schema version

Revision ID: 0006_event_schema_version
Revises: 0005_last_applied_revision
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_event_schema_version"
down_revision: str | None = "0005_last_applied_revision"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# This is the version understood when this migration was published. Keep the
# value local to the historical migration: importing a mutable application
# constant would silently change what an old database is backfilled with after
# a future domain-schema release.
EVENT_SCHEMA_VERSION = 1


def upgrade() -> None:
    # The temporary default safely fills rows written before envelopes carried
    # an explicit persisted version. New writes must always name their version,
    # so the default is removed immediately after the NOT NULL column exists.
    op.add_column(
        "events",
        sa.Column(
            "schema_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text(str(EVENT_SCHEMA_VERSION)),
        ),
    )
    op.alter_column(
        "events",
        "schema_version",
        existing_type=sa.Integer(),
        nullable=False,
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("events", "schema_version")
