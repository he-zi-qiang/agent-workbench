"""outbox claims become leases with a fencing token

Revision ID: 0003_outbox_lease
Revises: 0002_documents_outbox
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_outbox_lease"
down_revision: str | None = "0002_documents_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "outbox_events",
        sa.Column("claim_token", sa.String(length=128), nullable=True),
    )
    # Reclaim scans by expiry, so it must not walk the whole unacked backlog.
    op.create_index(
        "ix_outbox_events_lease",
        "outbox_events",
        ["lease_until"],
        unique=False,
        postgresql_where=sa.text("acked_at IS NULL"),
    )
    # Rows claimed before this migration have no lease and no token, so they
    # would be neither reclaimable nor acknowledgeable -- stuck exactly the way
    # this change exists to prevent. Releasing them is safe: an unacked event
    # is work still owed, and the worst case is that it is applied twice by an
    # ingestion side that has to be idempotent anyway.
    op.execute(
        sa.text(
            "UPDATE outbox_events SET claimed_by = NULL, claimed_at = NULL "
            "WHERE acked_at IS NULL AND claimed_at IS NOT NULL"
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_outbox_events_lease",
        table_name="outbox_events",
        postgresql_where=sa.text("acked_at IS NULL"),
    )
    op.drop_column("outbox_events", "claim_token")
    op.drop_column("outbox_events", "lease_until")
