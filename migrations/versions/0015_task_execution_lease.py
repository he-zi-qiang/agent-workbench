"""add Task claim leases, epochs and retry availability

Revision ID: 0015_task_execution_lease
Revises: 0014_task_input_fingerprint
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_task_execution_lease"
down_revision: str | None = "0014_task_input_fingerprint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("task_runs", sa.Column("lease_owner", sa.String(128), nullable=True))
    op.add_column(
        "task_runs",
        sa.Column("lease_epoch", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "task_runs", sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "task_runs",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "task_runs",
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "task_runs",
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # Pre-E1 Workers had no durable ownership token. An in-flight row cannot
    # honestly be preserved as running after this upgrade, so requeue it with
    # its checkpoint intact; the first E1 claim resumes that checkpoint under
    # a real epoch. This must happen before the lifecycle check is installed.
    op.execute(
        "UPDATE task_runs SET status = 'queued', status_detail = NULL, "
        "available_at = NOW() WHERE status = 'running'"
    )
    op.create_check_constraint(
        op.f("ck_task_runs_task_runs_lease_lifecycle"),
        "task_runs",
        "(status = 'running' AND lease_owner IS NOT NULL "
        "AND lease_until IS NOT NULL AND heartbeat_at IS NOT NULL) OR "
        "(status <> 'running' AND lease_owner IS NULL "
        "AND lease_until IS NULL AND heartbeat_at IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_task_runs_task_runs_lease_counters"),
        "task_runs",
        "lease_epoch >= 0 AND attempt_count >= 0",
    )
    op.create_index(
        "ix_task_runs_claim_eligible",
        "task_runs",
        ["available_at", "created_at", "task_id"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "ix_task_runs_expired_lease",
        "task_runs",
        ["lease_until", "task_id"],
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("ix_task_runs_expired_lease", table_name="task_runs")
    op.drop_index("ix_task_runs_claim_eligible", table_name="task_runs")
    op.drop_constraint(
        op.f("ck_task_runs_task_runs_lease_counters"), "task_runs", type_="check"
    )
    op.drop_constraint(
        op.f("ck_task_runs_task_runs_lease_lifecycle"), "task_runs", type_="check"
    )
    op.drop_column("task_runs", "available_at")
    op.drop_column("task_runs", "attempt_count")
    op.drop_column("task_runs", "heartbeat_at")
    op.drop_column("task_runs", "lease_until")
    op.drop_column("task_runs", "lease_epoch")
    op.drop_column("task_runs", "lease_owner")
