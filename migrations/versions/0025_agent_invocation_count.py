"""count agent invocations against the Task, across retries and reclaims

``multi_agent.max_agent_invocation_attempts_per_task`` has been declared since
the settings module existed and has never had a second reader. The reason it
was never projected is written down in ``bootstrap/projections.py``: it counts
attempts *across* retries and reclaims, so it needs a durable per-Task counter
rather than a number handed to a process, and projecting it without one would
have put it one import away from looking enforced.

This migration is that counter and nothing else. No code reads or writes the
column yet -- that is the next change, deliberately separated so a schema that
is merely present cannot be mistaken for a ceiling that is applied.

It lives on ``task_runs`` rather than in a ledger table. A ledger keyed by
``(task_id, agent_run_id)`` would look idempotent and would not be: the run id
is minted fresh on every replay, so the key buys diagnosability, not exactly
once. Saying so here matters because the alternative is a table whose name
promises something it cannot deliver.

Not nullable, defaulted to zero, and not backfilled in any other sense: every
row that exists now has spent an unknown amount, and zero is the only honest
starting point -- it under-counts history rather than inventing it. The
consequence is written in ADR-040: Tasks that predate this column get a full
allowance from here on.

The check constraint is extended rather than added beside, because the three
counters it now covers are the same kind of claim -- a count that cannot run
backwards -- and two constraints saying that about one row would drift.

Revision ID: 0025_agent_invocation_count
Revises: 0024_document_ingestion_failure
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_agent_invocation_count"
down_revision: str | None = "0024_document_ingestion_failure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COUNTERS = "task_runs_lease_counters"


def upgrade() -> None:
    op.add_column(
        "task_runs",
        sa.Column(
            "agent_invocation_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    # Replaced, not supplemented. The old constraint named two counters; the
    # row now has three of the same kind, and leaving the third outside would
    # make the constraint's name a lie the next reader has to check.
    op.drop_constraint(_COUNTERS, "task_runs", type_="check")
    op.create_check_constraint(
        _COUNTERS,
        "task_runs",
        "lease_epoch >= 0 AND attempt_count >= 0 AND agent_invocation_count >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(_COUNTERS, "task_runs", type_="check")
    op.create_check_constraint(
        _COUNTERS,
        "task_runs",
        "lease_epoch >= 0 AND attempt_count >= 0",
    )
    op.drop_column("task_runs", "agent_invocation_count")
