"""a Worker says it is here, and for how long that can be believed

One table: ``worker_presence`` (ADR-0110).

Until now the control plane could not see a Worker at all. ``task_runs`` carries
a ``heartbeat_at`` -- but only while a Task is claimed, so an idle Task Worker
and a dead one look identical from the API, and the ingestion Worker has no
row anywhere (its leases live on the outbox token). The console's 运行状态
page said so out loud: 「任务与文档 Worker——未知」 (known-gaps E-09).

**A row per process, upserted on a timer, with an expiry the writer sets.** The
reader compares ``expires_at`` with the *database* clock -- the same clock the
Task lease uses (``coordination.lease_time_source = postgresql_clock``), so a
Worker on a machine with a wrong clock is judged by the one clock every other
liveness question here is already judged by. A row past its expiry is not
deleted: "was here, stopped answering at 10:42" is a more useful fact than an
absent row, and the reader labels it stale rather than trusting it.

**Deployment label and capability snapshot, JSONB.** The console has to say
whether the Worker it sees is the ``--demo`` synthetic one and which graphs it
can build; those are facts the Worker knows at assembly and nobody else can
derive. Stored as the Worker reported them, read back as data.

**No foreign keys.** A Worker's identity is its own; nothing else references it.

Revision ID: 0033_worker_presence
Revises: 0032_events_stream_run_sequence
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0033_worker_presence"
down_revision: str | None = "0032_events_stream_run_sequence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "worker_presence",
        sa.Column("worker_id", sa.String(128), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("deployment", sa.String(128), nullable=False),
        sa.Column(
            "capabilities",
            JSONB(none_as_null=True),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('task', 'ingestion')", name="worker_presence_kind"
        ),
        sa.CheckConstraint(
            "expires_at > heartbeat_at", name="worker_presence_expiry_after_beat"
        ),
    )


def downgrade() -> None:
    # Losing this table loses nothing the system needs to run: it is a
    # readout, written by Workers and read by the console, and every Worker
    # rewrites its row within one heartbeat interval of the next upgrade.
    op.drop_table("worker_presence")
