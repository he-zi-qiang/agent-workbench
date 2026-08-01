"""add the external side-effect ledger

Revision ID: 0019_tool_executions
Revises: 0018_approvals
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_tool_executions"
down_revision: str | None = "0018_approvals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_executions",
        sa.Column("execution_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("operation_key", sa.String(length=128), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("canonical_request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("lease_epoch", sa.Integer(), nullable=False),
        sa.Column("agent_run_id", sa.String(length=128), nullable=False),
        sa.Column("tool_call_id", sa.String(length=128), nullable=False),
        sa.Column("policy_identity", sa.String(length=256), nullable=False),
        sa.Column("outcome_detail", sa.String(length=256), nullable=True),
        sa.Column(
            "intended_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('intended', 'succeeded', 'failed', 'needs_reconciliation')",
            name=op.f("ck_tool_executions_tool_executions_status"),
        ),
        sa.CheckConstraint(
            "(status = 'intended' AND settled_at IS NULL) OR "
            "(status <> 'intended' AND settled_at IS NOT NULL)",
            name=op.f("ck_tool_executions_tool_executions_settlement"),
        ),
        sa.CheckConstraint(
            "lease_epoch >= 1",
            name=op.f("ck_tool_executions_tool_executions_lease_epoch"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["task_runs.task_id"],
            name=op.f("fk_tool_executions_task_id"),
        ),
        sa.PrimaryKeyConstraint(
            "execution_id", name=op.f("pk_tool_executions_execution_id")
        ),
        sa.UniqueConstraint(
            "task_id",
            "operation_key",
            name=op.f("uq_tool_executions_task_id_operation_key"),
        ),
    )
    op.create_index(op.f("ix_tool_executions_task_id"), "tool_executions", ["task_id"])
    op.create_index(op.f("ix_tool_executions_status"), "tool_executions", ["status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_tool_executions_status"), table_name="tool_executions")
    op.drop_index(op.f("ix_tool_executions_task_id"), table_name="tool_executions")
    op.drop_table("tool_executions")
