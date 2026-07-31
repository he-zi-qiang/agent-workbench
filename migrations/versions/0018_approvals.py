"""add the approvals ledger and the resume reference it produces

Revision ID: 0018_approvals
Revises: 0017_qdrant_index_reservation
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_approvals"
down_revision: str | None = "0017_qdrant_index_reservation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("approval_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("graph_node_operation_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("decision_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name=op.f("ck_approvals_approvals_status"),
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND decision_version = 0 "
            "AND decided_by IS NULL AND decided_at IS NULL) OR "
            "(status <> 'pending' AND decision_version >= 1 "
            "AND decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name=op.f("ck_approvals_approvals_decision"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["task_runs.task_id"], name=op.f("fk_approvals_task_id")
        ),
        sa.PrimaryKeyConstraint("approval_id", name=op.f("pk_approvals")),
        sa.UniqueConstraint(
            "task_id",
            "graph_node_operation_id",
            name=op.f("uq_approvals_task_id_graph_node_operation_id"),
        ),
    )
    op.create_index("ix_approvals_task_id", "approvals", ["task_id"], unique=False)
    op.add_column(
        "task_runs", sa.Column("resume_kind", sa.String(length=16), nullable=True)
    )
    op.add_column(
        "task_runs",
        sa.Column("resume_approval_id", sa.String(length=128), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_task_runs_task_runs_resume_reference"),
        "task_runs",
        "(resume_kind IS NULL AND resume_approval_id IS NULL) OR "
        "(resume_kind = 'approval' AND resume_approval_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_task_runs_task_runs_resume_reference"), "task_runs", type_="check"
    )
    op.drop_column("task_runs", "resume_approval_id")
    op.drop_column("task_runs", "resume_kind")
    op.drop_index("ix_approvals_task_id", table_name="approvals")
    op.drop_table("approvals")
