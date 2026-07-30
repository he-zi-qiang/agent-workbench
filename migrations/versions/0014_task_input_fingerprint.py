"""identify Task submissions by immutable input content

Revision ID: 0014_task_input_fingerprint
Revises: 0013_task_tenant_dedup

Artifact ids are generated on every write.  A retry may therefore store the
same TaskInput bytes under a different id; idempotency must compare its
canonical SHA-256 fingerprint, while keeping the first input_ref as the Task's
recoverable source of truth.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_task_input_fingerprint"
down_revision: str | None = "0013_task_tenant_dedup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "task_runs",
        sa.Column("input_fingerprint", sa.String(length=64), nullable=True),
    )
    # Rows written before TaskInput existed only retain their immutable
    # input_ref. Backfill the documented direct-caller fallback in Python so
    # this migration does not depend on PostgreSQL's optional pgcrypto module.
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT task_id, input_ref FROM task_runs")).mappings()
    for row in rows:
        fingerprint = hashlib.sha256(row["input_ref"].encode("utf-8")).hexdigest()
        bind.execute(
            sa.text(
                "UPDATE task_runs SET input_fingerprint = :fingerprint "
                "WHERE task_id = :task_id"
            ),
            {"task_id": row["task_id"], "fingerprint": fingerprint},
        )
    op.alter_column("task_runs", "input_fingerprint", nullable=False)


def downgrade() -> None:
    op.drop_column("task_runs", "input_fingerprint")
