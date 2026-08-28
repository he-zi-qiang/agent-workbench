"""a tree of runs is read as a tree

One index on `events`: `(stream_id, run_id, sequence)` (ADR-083).

A stream has held several runs since Chat existed -- a session is the stream and
each turn is a run -- but nothing ever asked for one of them, so `events` carried
only `(stream_id, sequence)`. Delegation (ADR-082) makes the question real: a
delegated run writes into its parent's stream under its own `run_id`, so "show me
only what this sub-agent did" is now something a person will click on.

**Why all three columns, and in this order.** `stream_id` first because every
read is scoped to one stream and authorization is answered there. `run_id`
second because it is the equality this index exists for. `sequence` last because
the read is *ordered and cursored* by it: without it on the end the planner can
find the rows but not their order, and a page of twelve events costs a sort of
the whole stream. That is the difference between this index being worth adding
and it being decoration -- `tests/persistence/test_run_tree_index.py` asserts the
plan rather than trusting it.

**Not unique.** One run writes many events; that is the point.

**No backfill, and none possible to need.** `run_id` has been `NOT NULL` since
`0004_event_log`, so every existing row already carries the value this index is
built over. The index is created over the table as it stands.

Revision ID: 0032_events_stream_run_sequence
Revises: 0031_project_root_path
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0032_events_stream_run_sequence"
down_revision: str | None = "0031_project_root_path"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "ix_events_stream_run_sequence"


def upgrade() -> None:
    op.create_index(INDEX_NAME, "events", ["stream_id", "run_id", "sequence"])


def downgrade() -> None:
    # Losing an index loses no data, which makes this one of the few downgrades
    # in this chain that costs nothing but speed. The narrowed read keeps
    # working; it goes back to scanning the stream.
    op.drop_index(INDEX_NAME, table_name="events")
