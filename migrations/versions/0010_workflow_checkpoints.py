"""add the tables a LangGraph checkpoint saver writes

Revision ID: 0010_workflow_checkpoints
Revises: 0009_chat_turn_lease
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_workflow_checkpoints"
down_revision: str | None = "0009_chat_turn_lease"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_checkpoints",
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), nullable=False),
        sa.Column("checkpoint_id", sa.Text(), nullable=False),
        sa.Column("parent_checkpoint_id", sa.Text(), nullable=True),
        sa.Column("payload_type", sa.Text(), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            name=op.f("pk_workflow_checkpoints"),
        ),
    )
    op.create_table(
        "workflow_checkpoint_blobs",
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("payload_type", sa.Text(), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "thread_id",
            "checkpoint_ns",
            "channel",
            "version",
            name=op.f("pk_workflow_checkpoint_blobs"),
        ),
    )
    op.create_table(
        "workflow_checkpoint_writes",
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), nullable=False),
        sa.Column("checkpoint_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("task_path", sa.Text(), nullable=False),
        sa.Column("payload_type", sa.Text(), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # No foreign key to workflow_checkpoints: under the default
        # durability mode LangGraph issues a step's writes without awaiting
        # the checkpoint put, so the referenced row is routinely not there yet.
        sa.PrimaryKeyConstraint(
            "thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            "task_id",
            "idx",
            name=op.f("pk_workflow_checkpoint_writes"),
        ),
    )


def downgrade() -> None:
    op.drop_table("workflow_checkpoint_writes")
    op.drop_table("workflow_checkpoint_blobs")
    op.drop_table("workflow_checkpoints")
