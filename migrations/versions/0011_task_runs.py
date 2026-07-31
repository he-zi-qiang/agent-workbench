"""add the Task Registry's product lifecycle

Revision ID: 0011_task_runs
Revises: 0010_workflow_checkpoints
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_task_runs"
down_revision: str | None = "0010_workflow_checkpoints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_runs",
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("graph_version", sa.String(length=64), nullable=False),
        sa.Column("input_ref", sa.String(length=128), nullable=False),
        sa.Column("submission_dedup_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("status_detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN "
            "('queued', 'running', 'waiting_approval', 'waiting_migration', "
            "'succeeded', 'failed', 'cancelled', 'dead_letter')",
            name=op.f("ck_task_runs_task_runs_status"),
        ),
        sa.CheckConstraint(
            "(status IN ('waiting_migration', 'failed', 'cancelled', 'dead_letter') "
            "AND status_detail IS NOT NULL) OR "
            "(status IN ('queued', 'running', 'waiting_approval', 'succeeded') "
            "AND status_detail IS NULL)",
            name=op.f("ck_task_runs_task_runs_status_detail"),
        ),
        sa.PrimaryKeyConstraint("task_id", name=op.f("pk_task_runs")),
        sa.UniqueConstraint("thread_id", name=op.f("uq_task_runs_thread_id")),
        sa.UniqueConstraint(
            "owner_id",
            "submission_dedup_key",
            name=op.f("uq_task_runs_owner_id_submission_dedup_key"),
        ),
    )
    op.create_index(
        "ix_task_runs_queued",
        "task_runs",
        ["created_at", "task_id"],
        unique=False,
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "ix_task_runs_tenant_id_task_id",
        "task_runs",
        ["tenant_id", "task_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_task_runs_tenant_id_task_id", table_name="task_runs")
    op.drop_index("ix_task_runs_queued", table_name="task_runs")
    op.drop_table("task_runs")
