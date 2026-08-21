"""a project is a membership, not a container

New tables `projects` and `project_knowledge_bases`, plus one nullable
`project_id` column on each of `conversation_sessions` and `task_runs`.

**This migration writes no data.** Every existing row keeps `project_id = NULL`;
not one is swept into a project. That is ADR-071 2.1 landing in a migration:
inventing a project for each person and filing their whole history under it
would lie twice over -- claiming those things belong to one piece of work, and
claiming that was the user's judgement.

The foreign keys are ON DELETE SET NULL (project_id), not CASCADE. A project is
a label, and deleting a label must not delete what it labelled (ADR-071 2.2).
Naming the column is load-bearing: a composite foreign key nulls every column it
covers, so a bare SET NULL would blank tenant_id too -- which is NOT NULL, so
deleting a project raised a not-null violation instead of releasing its members.
The contract suite against real PostgreSQL is what caught that. Knowledge bases
join through a link table instead of a column because one of them is used by
several pieces of work at once (2.3).

Both foreign keys are composite with tenant on each side: single-column ones
would admit a row linking tenant A's project to tenant B's knowledge base --
well-typed, and meaningless in this system.

Revision ID: 0030_projects
Revises: 0029_succeeded_carries_caveat
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_projects"
down_revision: str | None = "0029_succeeded_carries_caveat"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_IDENTIFIER_LENGTH = 128
_PROJECT_NAME_LENGTH = 200


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("project_id", sa.String(_IDENTIFIER_LENGTH), primary_key=True),
        sa.Column("tenant_id", sa.String(_IDENTIFIER_LENGTH), primary_key=True),
        sa.Column("owner_id", sa.String(_IDENTIFIER_LENGTH), nullable=False),
        sa.Column("name", sa.String(_PROJECT_NAME_LENGTH), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_projects_tenant_id_owner_id_archived_at",
        "projects",
        ["tenant_id", "owner_id", "archived_at", sa.text("updated_at DESC")],
    )

    op.create_table(
        "project_knowledge_bases",
        sa.Column("tenant_id", sa.String(_IDENTIFIER_LENGTH), primary_key=True),
        sa.Column("project_id", sa.String(_IDENTIFIER_LENGTH), primary_key=True),
        sa.Column("knowledge_base_id", sa.String(_IDENTIFIER_LENGTH), primary_key=True),
        sa.Column(
            "linked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "tenant_id"],
            ["projects.project_id", "projects.tenant_id"],
            ondelete="CASCADE",
            name="fk_project_knowledge_bases_project",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id", "tenant_id"],
            ["knowledge_bases.knowledge_base_id", "knowledge_bases.tenant_id"],
            ondelete="CASCADE",
            name="fk_project_knowledge_bases_knowledge_base",
        ),
    )
    op.create_index(
        "ix_project_knowledge_bases_tenant_id_knowledge_base_id",
        "project_knowledge_bases",
        ["tenant_id", "knowledge_base_id"],
    )

    for table, fk_name, index_name in (
        (
            "conversation_sessions",
            "fk_conversation_sessions_project",
            "ix_conversation_sessions_tenant_id_project_id",
        ),
        ("task_runs", "fk_task_runs_project", "ix_task_runs_tenant_id_project_id"),
    ):
        # Nullable, with no backfill and no default. Every existing row keeps
        # NULL, which is the honest answer: nobody has said what these belong to.
        op.add_column(
            table,
            sa.Column("project_id", sa.String(_IDENTIFIER_LENGTH), nullable=True),
        )
        op.create_foreign_key(
            fk_name,
            table,
            "projects",
            ["project_id", "tenant_id"],
            ["project_id", "tenant_id"],
            # Naming the column matters: a composite foreign key nulls every
            # column it covers, so a bare SET NULL would blank tenant_id too --
            # and that is NOT NULL, so deleting a project raised a not-null
            # violation instead of releasing its members. PostgreSQL 15+.
            ondelete="SET NULL (project_id)",
        )
        op.create_index(index_name, table, ["tenant_id", "project_id"])


def downgrade() -> None:
    for table, fk_name, index_name in (
        ("task_runs", "fk_task_runs_project", "ix_task_runs_tenant_id_project_id"),
        (
            "conversation_sessions",
            "fk_conversation_sessions_project",
            "ix_conversation_sessions_tenant_id_project_id",
        ),
    ):
        op.drop_index(index_name, table_name=table)
        op.drop_constraint(fk_name, table, type_="foreignkey")
        op.drop_column(table, "project_id")

    op.drop_index(
        "ix_project_knowledge_bases_tenant_id_knowledge_base_id",
        table_name="project_knowledge_bases",
    )
    op.drop_table("project_knowledge_bases")
    op.drop_index("ix_projects_tenant_id_owner_id_archived_at", table_name="projects")
    op.drop_table("projects")
