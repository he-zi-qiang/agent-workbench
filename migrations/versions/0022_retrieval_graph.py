"""entity and relationship rows that nominate chunks, with per-mention provenance

ADR-037. Retrieval gains two arms that reach a chunk through an entity or a
relationship rather than through the query's own words -- which is what the
2026-08-10 falsification showed hybrid could not do: half the cross-document
questions came back holding one of their two documents.

Entities merge inside one knowledge base so two documents naming the same
thing become one way in. Evidence does not merge: ``kg_mentions`` and the
provenance columns on ``kg_relations`` point at the exact chunk each claim was
read from, and retrieval nominates *those chunks*. A merged node built out of
two documents could not answer "may this principal read it", and the ACL
re-check is by document -- which is the whole reason this is not the merged
knowledge graph LightRAG builds.

``outbox_events.kind`` widens by one value. The second extraction pass is
claimed, leased, heartbeaten and retried by the machinery already there rather
than by a scheduler of its own.

Revision ID: 0022_retrieval_graph
Revises: 0021_task_objective_preview
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_retrieval_graph"
down_revision: str | None = "0021_task_objective_preview"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_IDENTIFIER = 128

_KIND_WITH_GRAPH = (
    "kind IN ('document_upserted', 'document_deleted', 'acl_changed', "
    "'graph_extraction_requested')"
)
_KIND_WITHOUT_GRAPH = "kind IN ('document_upserted', 'document_deleted', 'acl_changed')"


def upgrade() -> None:
    op.create_table(
        "kg_entities",
        sa.Column("entity_id", sa.String(length=_IDENTIFIER), primary_key=True),
        sa.Column("tenant_id", sa.String(length=_IDENTIFIER), nullable=False),
        sa.Column("knowledge_base_id", sa.String(length=_IDENTIFIER), nullable=False),
        sa.Column("normalized_name", sa.String(length=512), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=512), nullable=False),
        sa.Column("graph_identity", sa.String(length=256), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "knowledge_base_id",
            "normalized_name",
            "entity_type",
            "graph_identity",
            name="uq_kg_entities_merge_key",
        ),
    )

    op.create_table(
        "kg_mentions",
        sa.Column("mention_id", sa.String(length=_IDENTIFIER), primary_key=True),
        sa.Column(
            "entity_id",
            sa.String(length=_IDENTIFIER),
            sa.ForeignKey("kg_entities.entity_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(length=_IDENTIFIER), nullable=False),
        sa.Column("knowledge_base_id", sa.String(length=_IDENTIFIER), nullable=False),
        sa.Column("document_id", sa.String(length=_IDENTIFIER), nullable=False),
        sa.Column("document_version", sa.String(length=_IDENTIFIER), nullable=False),
        sa.Column("chunk_id", sa.String(length=_IDENTIFIER), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "entity_id", "chunk_id", name="uq_kg_mentions_entity_chunk"
        ),
    )
    op.create_index("ix_kg_mentions_entity_id", "kg_mentions", ["entity_id"])
    op.create_index(
        "ix_kg_mentions_document", "kg_mentions", ["tenant_id", "document_id"]
    )

    op.create_table(
        "kg_relations",
        sa.Column("relation_id", sa.String(length=_IDENTIFIER), primary_key=True),
        sa.Column("tenant_id", sa.String(length=_IDENTIFIER), nullable=False),
        sa.Column("knowledge_base_id", sa.String(length=_IDENTIFIER), nullable=False),
        sa.Column(
            "subject_entity_id",
            sa.String(length=_IDENTIFIER),
            sa.ForeignKey("kg_entities.entity_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "object_entity_id",
            sa.String(length=_IDENTIFIER),
            sa.ForeignKey("kg_entities.entity_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("description", sa.String(length=2048), nullable=False),
        sa.Column("document_id", sa.String(length=_IDENTIFIER), nullable=False),
        sa.Column("document_version", sa.String(length=_IDENTIFIER), nullable=False),
        sa.Column("chunk_id", sa.String(length=_IDENTIFIER), nullable=False),
        sa.Column("graph_identity", sa.String(length=256), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "subject_entity_id",
            "object_entity_id",
            "chunk_id",
            name="uq_kg_relations_edge_chunk",
        ),
    )
    op.create_index(
        "ix_kg_relations_document", "kg_relations", ["tenant_id", "document_id"]
    )

    op.drop_constraint("outbox_events_kind", "outbox_events", type_="check")
    op.create_check_constraint("outbox_events_kind", "outbox_events", _KIND_WITH_GRAPH)


def downgrade() -> None:
    # Unacked extraction requests go first. Narrowing the constraint while one
    # is still queued would leave a row the constraint forbids, and the
    # migration would fail on data it wrote itself.
    op.execute("DELETE FROM outbox_events WHERE kind = 'graph_extraction_requested'")
    op.drop_constraint("outbox_events_kind", "outbox_events", type_="check")
    op.create_check_constraint(
        "outbox_events_kind", "outbox_events", _KIND_WITHOUT_GRAPH
    )

    op.drop_index("ix_kg_relations_document", table_name="kg_relations")
    op.drop_table("kg_relations")
    op.drop_index("ix_kg_mentions_document", table_name="kg_mentions")
    op.drop_index("ix_kg_mentions_entity_id", table_name="kg_mentions")
    op.drop_table("kg_mentions")
    op.drop_table("kg_entities")
