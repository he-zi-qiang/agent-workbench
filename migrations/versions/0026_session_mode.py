"""a conversation session says which API is allowed to drive it

Chat and Code share ``conversation_sessions`` and ``conversation_messages``
because they share an identity -- one principal, one tenant, one ordered
history. They do not share a lifecycle. Chat publishes through ``chat_turns``
(claim, lease, release_pending, assistant message); Code writes no turn row at
all, because its product is a workspace and a report rather than an answer that
must pass a release fence.

That asymmetry is exactly why this column has to exist. Without it a session id
is enough to drive a Code session through the Chat API, and the Chat API would
happily claim a turn on it -- taking a lease that only the expiration reaper
can return, and eventually appending an assistant message that no
``AnswerCommitted`` ever authorized.

``server_default 'chat'`` and no backfill statement are the same decision: every
row that exists when this runs was created by the Chat API, so the default is
the historical truth rather than a guess. A nullable column would have been the
guess -- it pushes "what may a session with no mode do?" into every reader.

The CHECK is here rather than only in the Pydantic layer because the value's
whole job is to be a gate. A gate that can be bypassed by any writer that skips
the repository is a gate in the documentation only.

Revision ID: 0026_session_mode
Revises: 0025_agent_invocation_count
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_session_mode"
down_revision: str | None = "0025_agent_invocation_count"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The bare name, as everywhere else here: the metadata's naming convention
# expands it on both sides, so writing the expanded form would double the
# prefix and drop a constraint that does not exist.
_MODE_CHECK = "conversation_sessions_mode"


def upgrade() -> None:
    op.add_column(
        "conversation_sessions",
        sa.Column(
            "mode",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'chat'"),
        ),
    )
    op.create_check_constraint(
        _MODE_CHECK,
        "conversation_sessions",
        "mode IN ('chat', 'code')",
    )


def downgrade() -> None:
    op.drop_constraint(_MODE_CHECK, "conversation_sessions", type_="check")
    op.drop_column("conversation_sessions", "mode")
