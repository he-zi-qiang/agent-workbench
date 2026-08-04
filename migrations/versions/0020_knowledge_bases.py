"""make knowledge bases durable product entities

Revision ID: 0020_knowledge_bases
Revises: 0019_tool_executions
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_knowledge_bases"
down_revision: str | None = "0019_tool_executions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_bases",
        sa.Column("knowledge_base_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint(
            "knowledge_base_id",
            "tenant_id",
            name=op.f("pk_knowledge_bases"),
        ),
    )
    op.create_index(
        "ix_knowledge_bases_tenant_id_owner_id_created_at",
        "knowledge_bases",
        ["tenant_id", "owner_id", "created_at"],
        unique=False,
    )

    # Before this migration a knowledge base was only the string repeated on
    # document rows. Preserve every existing scope. If legacy rows put several
    # owners in the same tenant/base, the earliest document deterministically
    # supplies the creator; document ACLs still preserve read discovery, while
    # future writes become owner-only as the new contract requires.
    op.execute(
        sa.text(
            """
            INSERT INTO knowledge_bases (
                knowledge_base_id,
                tenant_id,
                owner_id,
                name,
                description,
                created_at,
                updated_at
            )
            SELECT DISTINCT ON (tenant_id, knowledge_base_id)
                knowledge_base_id,
                tenant_id,
                owner_id,
                knowledge_base_id,
                NULL,
                created_at,
                created_at
            FROM documents
            ORDER BY tenant_id, knowledge_base_id, created_at, document_id
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_knowledge_bases_tenant_id_owner_id_created_at",
        table_name="knowledge_bases",
    )
    op.drop_table("knowledge_bases")
