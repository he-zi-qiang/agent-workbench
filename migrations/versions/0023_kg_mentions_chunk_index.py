"""index kg_mentions by chunk, for seed expansion

ADR-037 §2.7. The graph arm turned out to run the opposite way round from the
one §2.1 drew: the bridge entity is named in the document the other arms
already found, not in the query -- measured 0/7 against 7/7 on the
cross-document failures. So the hot lookup is "given these chunks, which
entities did they name", and the existing indexes both read the other way.

Nothing else changes. The rows, their constraints and every other query are
what 0022 created.

Revision ID: 0023_kg_mentions_chunk_index
Revises: 0022_retrieval_graph
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0023_kg_mentions_chunk_index"
down_revision: str | None = "0022_retrieval_graph"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_kg_mentions_chunk",
        "kg_mentions",
        ["tenant_id", "knowledge_base_id", "chunk_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_kg_mentions_chunk", table_name="kg_mentions")
