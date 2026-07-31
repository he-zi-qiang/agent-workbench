"""reserve a concrete Qdrant index generation for a Task

Revision ID: 0017_qdrant_index_reservation
Revises: 0016_task_principal_scopes

The generation table is created here because a reservation cannot exist without
something to reserve. Only what a reservation needs is created: a generation's
backfill progress, readiness and retention windows belong to the ingestion state
the plan assigns to WP04-05, and inventing columns for them here would be
guessing at that design.

The three ``task_runs`` columns are nullable because a Task that uses no
knowledge base reserves nothing. They are all-or-nothing rather than
independently optional -- half a reservation is a snapshot nobody can act on.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_qdrant_index_reservation"
down_revision: str | None = "0016_task_principal_scopes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "qdrant_index_generations",
        sa.Column("generation_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("collection_name", sa.String(length=128), nullable=False),
        sa.Column("index_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'draining', 'retired')",
            name=op.f("ck_qdrant_index_generations_qdrant_index_generations_status"),
        ),
        sa.PrimaryKeyConstraint(
            "generation_id", name=op.f("pk_qdrant_index_generations")
        ),
        sa.UniqueConstraint(
            "collection_name",
            "index_version",
            name=op.f("uq_qdrant_index_generations_collection_name_index_version"),
        ),
    )
    op.create_index(
        "uq_qdrant_index_generations_active",
        "qdrant_index_generations",
        ["collection_name"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.add_column(
        "task_runs",
        sa.Column("resolved_qdrant_collection", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "task_runs",
        sa.Column("resolved_qdrant_index_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "task_runs",
        sa.Column(
            "resolved_qdrant_index_generation_id",
            postgresql.UUID(as_uuid=False),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        op.f("fk_task_runs_resolved_qdrant_index_generation_id"),
        "task_runs",
        "qdrant_index_generations",
        ["resolved_qdrant_index_generation_id"],
        ["generation_id"],
    )
    op.create_check_constraint(
        op.f("ck_task_runs_task_runs_resolved_index"),
        "task_runs",
        "(resolved_qdrant_collection IS NULL "
        "AND resolved_qdrant_index_version IS NULL "
        "AND resolved_qdrant_index_generation_id IS NULL) OR "
        "(resolved_qdrant_collection IS NOT NULL "
        "AND resolved_qdrant_index_version IS NOT NULL "
        "AND resolved_qdrant_index_generation_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_task_runs_task_runs_resolved_index"), "task_runs", type_="check"
    )
    op.drop_constraint(
        op.f("fk_task_runs_resolved_qdrant_index_generation_id"),
        "task_runs",
        type_="foreignkey",
    )
    op.drop_column("task_runs", "resolved_qdrant_index_generation_id")
    op.drop_column("task_runs", "resolved_qdrant_index_version")
    op.drop_column("task_runs", "resolved_qdrant_collection")
    op.drop_index(
        "uq_qdrant_index_generations_active", table_name="qdrant_index_generations"
    )
    op.drop_table("qdrant_index_generations")
