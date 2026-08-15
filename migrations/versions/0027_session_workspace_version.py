"""a session remembers which version of its working set it is at

A Task never needed this column. Its workspace version travels through graph
state: a node is handed the version pinned at its entry and publishes a new one
only by returning a state update, so a node that dies publishes nothing and the
attempt that replaces it reads the same entry version. The half-finished writes
stay in the artifact store, unreachable, because no manifest anybody holds names
them.

A Code session has no graph to carry that. Its run is one process doing one
turn, and when the turn is cancelled or the process dies there is no state
update to withhold and no retry to hand an entry version to. Somewhere has to
hold the pointer across turns, and the session row is the only thing in this
system whose lifetime is the session's.

Nullable, and NULL is a value rather than an absence: it names the session that
has not written a file yet. That is the state every session starts in, so it is
also a state the compare-and-set above this column has to be able to say out
loud -- a writer whose comparison cannot express "still at no version" would
refuse every session's first write.

There is deliberately no foreign key. The value is an artifact id, and artifacts
live in a store this database does not own; a referential constraint here could
only ever be enforced somewhere else, which makes it a claim rather than a
constraint.

Revision ID: 0027_session_workspace_version
Revises: 0026_session_mode
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_session_workspace_version"
down_revision: str | None = "0026_session_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Matches ``IDENTIFIER_LENGTH`` in ``adapters/persistence/models.py``. Spelled
#: out rather than imported: a migration has to keep describing the change it
#: made even after the constant moves under it.
_IDENTIFIER_LENGTH = 128


def upgrade() -> None:
    op.add_column(
        "conversation_sessions",
        sa.Column(
            "workspace_version",
            sa.String(length=_IDENTIFIER_LENGTH),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("conversation_sessions", "workspace_version")
