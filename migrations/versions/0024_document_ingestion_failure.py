"""record the revision an ingestion attempt refused, and why

A document status was derived from two numbers: the revision that exists and
the revision the index has been told about. Those two say "not indexed yet".
They cannot say "will never be indexed", so a file no parser in this build can
read was reported as processing for as long as anybody kept looking, while the
outbox retried it after every lease expiry.

Nullable and not backfilled. A failure is an observation an ingestion attempt
makes, and no attempt was ever recorded for the rows that exist now; inventing
one here would mark documents failed that nobody has refused. Rows written
before this migration keep the status they already had, and the next attempt
records the truth.

The check constraint is the point of storing two columns rather than one: a
revision with no code says nothing to the reader, and a code with no revision
belongs to no revision -- so neither half exists alone.

Revision ID: 0024_document_ingestion_failure
Revises: 0023_kg_mentions_chunk_index
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_document_ingestion_failure"
down_revision: str | None = "0023_kg_mentions_chunk_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("failed_revision", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column("failure_code", sa.String(length=32), nullable=True),
    )
    op.create_check_constraint(
        "documents_failure_is_whole",
        "documents",
        "(failed_revision IS NULL) = (failure_code IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("documents_failure_is_whole", "documents", type_="check")
    op.drop_column("documents", "failure_code")
    op.drop_column("documents", "failed_revision")
