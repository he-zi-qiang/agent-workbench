"""a session remembers when somebody last said something in it

`created_at` already exists and is the wrong clock for a list. Ordering by it
puts a session untouched for a month above the one you were in five minutes
ago, which is backwards for a list whose only job is getting a person back to
where they were.

NOT NULL with a default rather than nullable, and the backfill is what makes
that possible. A nullable column would need `COALESCE(last_activity_at,
created_at)` in the ordering, and a COALESCE'd expression cannot use the index
this migration adds -- so the nullable version would be the one that is both
harder to read and slower.

Existing rows are backfilled from `created_at` rather than from `now()`. Both
are lies about when those conversations actually happened, but one of them
sorts a five-year-old session above today's, and the other keeps the rows in
the only order this database still knows about them.

The index carries the exact shape of the list query -- one tenant, one owner,
newest activity first -- because that query is the entire reason the column
exists.

Revision ID: 0028_session_last_activity
Revises: 0027_session_workspace_version
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_session_last_activity"
down_revision: str | None = "0027_session_workspace_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_conversation_sessions_tenant_owner_activity"


def upgrade() -> None:
    # Added nullable, backfilled, then tightened. Adding it NOT NULL in one
    # step would need a server default applied to every existing row anyway,
    # and would stamp them all with the deploy time rather than with what this
    # database knows.
    op.add_column(
        "conversation_sessions",
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE conversation_sessions SET last_activity_at = created_at "
        "WHERE last_activity_at IS NULL"
    )
    op.alter_column(
        "conversation_sessions",
        "last_activity_at",
        nullable=False,
        server_default=sa.func.now(),
    )
    op.create_index(
        _INDEX,
        "conversation_sessions",
        ["tenant_id", "owner_id", sa.text("last_activity_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="conversation_sessions")
    op.drop_column("conversation_sessions", "last_activity_at")
