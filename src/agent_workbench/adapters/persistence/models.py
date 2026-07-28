"""The relational schema, expressed once.

These are Core tables rather than ORM classes. A repository's job here is to
turn rows into domain objects and back, explicitly; an identity map and lazy
loading would add a second, implicit notion of when a read happens, which is
the last thing a store whose ordering guarantees matter needs.

This metadata is also what Alembic compares against. A migration that drifts
from these definitions is a schema nobody described, so a test asserts the two
agree rather than trusting that they were changed together.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

# Explicit naming keeps generated constraint names stable across databases, so
# a migration can drop by name what an earlier one created by name.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)

IDENTIFIER_LENGTH = 128

conversation_sessions = Table(
    "conversation_sessions",
    metadata,
    Column("session_id", String(IDENTIFIER_LENGTH), primary_key=True),
    Column("tenant_id", String(IDENTIFIER_LENGTH), nullable=False),
    Column("owner_id", String(IDENTIFIER_LENGTH), nullable=False),
    Column("title", String(256), nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    # Every query carries the tenant, so the tenant leads the index.
    Index("ix_conversation_sessions_tenant_id_session_id", "tenant_id", "session_id"),
)

messages = Table(
    "messages",
    metadata,
    Column("message_id", String(IDENTIFIER_LENGTH), primary_key=True),
    Column(
        "session_id",
        String(IDENTIFIER_LENGTH),
        ForeignKey("conversation_sessions.session_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("sequence", Integer, nullable=False),
    # The serialized domain Message, schema version included. Reading it back
    # through the domain model means a row written by a contract this process
    # does not know fails closed instead of being half-understood.
    Column("payload", JSONB, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    # Gap-free ordering is a promise of the port, so the database enforces it:
    # a racing appender collides here instead of silently reusing a position.
    UniqueConstraint("session_id", "sequence", name="uq_messages_session_id_sequence"),
    Index("ix_messages_session_id_sequence", "session_id", "sequence"),
)

DIGEST_LENGTH = 64
FILENAME_LENGTH = 255

artifacts = Table(
    "artifacts",
    metadata,
    Column("artifact_id", String(IDENTIFIER_LENGTH), primary_key=True),
    Column("tenant_id", String(IDENTIFIER_LENGTH), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("media_type", String(128), nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("sha256", String(DIGEST_LENGTH), nullable=False),
    Column("filename", String(FILENAME_LENGTH), nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Index("ix_artifacts_tenant_id_artifact_id", "tenant_id", "artifact_id"),
)

upload_intents = Table(
    "upload_intents",
    metadata,
    Column("upload_id", String(IDENTIFIER_LENGTH), primary_key=True),
    Column("tenant_id", String(IDENTIFIER_LENGTH), nullable=False),
    Column("owner_id", String(IDENTIFIER_LENGTH), nullable=False),
    # What the client promised before it transferred anything. Completion
    # compares the stored object against these, so a transfer that delivered
    # something else cannot become a document version.
    Column("declared_size_bytes", BigInteger, nullable=False),
    Column("declared_sha256", String(DIGEST_LENGTH), nullable=False),
    Column("media_type", String(128), nullable=False),
    Column("filename", String(FILENAME_LENGTH), nullable=True),
    Column("status", String(16), nullable=False),
    # Set when the intent is completed, which is what makes completing the same
    # upload twice return the same version instead of creating a second one.
    Column("version_id", String(IDENTIFIER_LENGTH), nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    CheckConstraint(
        "status IN ('pending', 'completed')",
        name="upload_intents_status",
    ),
    Index("ix_upload_intents_tenant_id_upload_id", "tenant_id", "upload_id"),
)

documents = Table(
    "documents",
    metadata,
    Column("document_id", String(IDENTIFIER_LENGTH), primary_key=True),
    Column("tenant_id", String(IDENTIFIER_LENGTH), nullable=False),
    Column("owner_id", String(IDENTIFIER_LENGTH), nullable=False),
    Column("knowledge_base_id", String(IDENTIFIER_LENGTH), nullable=False),
    # Monotonic per document. A stale outbox event carries an older value and
    # is discarded by the worker rather than applied over newer content.
    Column("source_revision", BigInteger, nullable=False),
    # What the index has already been told about this document. Compared
    # against an event's revision so a delayed event can be recognised as
    # describing a past state rather than applied over a newer one -- a stable
    # point id stops duplicate writes, and only this stops out-of-order ones.
    Column("last_applied_revision", BigInteger, nullable=False, server_default="0"),
    Column("deleted", Boolean, nullable=False, server_default=text("false")),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Index(
        "ix_documents_tenant_id_knowledge_base_id",
        "tenant_id",
        "knowledge_base_id",
    ),
)

document_versions = Table(
    "document_versions",
    metadata,
    Column("version_id", String(IDENTIFIER_LENGTH), primary_key=True),
    Column(
        "document_id",
        String(IDENTIFIER_LENGTH),
        ForeignKey("documents.document_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("source_revision", BigInteger, nullable=False),
    Column("artifact_id", String(IDENTIFIER_LENGTH), nullable=False),
    Column("content_sha256", String(DIGEST_LENGTH), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    UniqueConstraint(
        "document_id",
        "source_revision",
        name="uq_document_versions_document_id_source_revision",
    ),
)

document_acl = Table(
    "document_acl",
    metadata,
    Column(
        "document_id",
        String(IDENTIFIER_LENGTH),
        ForeignKey("documents.document_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("principal_id", String(IDENTIFIER_LENGTH), primary_key=True),
)

outbox_events = Table(
    "outbox_events",
    metadata,
    # The database assigns the position, so ordering does not depend on any
    # producer's clock or on the order two transactions happened to start in.
    Column("sequence", BigInteger, Identity(always=True), primary_key=True),
    Column("event_id", String(IDENTIFIER_LENGTH), nullable=False, unique=True),
    # No foreign key to documents on purpose: a deletion event has to outlive
    # the row it describes, otherwise the index can never be told to forget it.
    Column("document_id", String(IDENTIFIER_LENGTH), nullable=False),
    Column("source_revision", BigInteger, nullable=False),
    Column("kind", String(32), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column("claimed_by", String(IDENTIFIER_LENGTH), nullable=True),
    Column("claimed_at", DateTime(timezone=True), nullable=True),
    # A claim is a lease, not a possession. It expires so a worker that dies
    # holding one does not take its share of the queue with it.
    Column("lease_until", DateTime(timezone=True), nullable=True),
    # The fence. Every claim mints a new one, so an acknowledgement from a
    # worker whose lease was already reclaimed matches nothing and is refused
    # rather than silently marking somebody else's work done.
    Column("claim_token", String(IDENTIFIER_LENGTH), nullable=True),
    Column("acked_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "kind IN ('document_upserted', 'document_deleted', 'acl_changed')",
        name="outbox_events_kind",
    ),
    Index(
        "ix_outbox_events_pending",
        "sequence",
        postgresql_where=text("acked_at IS NULL"),
    ),
    # Reclaim scans by expiry, so it must not walk the whole unacked backlog.
    Index(
        "ix_outbox_events_lease",
        "lease_until",
        postgresql_where=text("acked_at IS NULL"),
    ),
)


__all__ = [
    "DIGEST_LENGTH",
    "FILENAME_LENGTH",
    "IDENTIFIER_LENGTH",
    "NAMING_CONVENTION",
    "artifacts",
    "conversation_sessions",
    "document_acl",
    "document_versions",
    "documents",
    "messages",
    "metadata",
    "outbox_events",
    "upload_intents",
]


# The stream row exists to be locked. A sequence has to be gap-free within its
# stream for a cursor to mean "everything up to here", and the only way to get
# that under concurrency is to serialise appends behind something -- an
# Identity column would be unique and full of holes, because a rolled-back
# transaction consumes a value it never writes.
event_streams = Table(
    "event_streams",
    metadata,
    Column("stream_id", String(IDENTIFIER_LENGTH), primary_key=True),
    # No tenant column. A stream's tenant is not on EventScope, and a column
    # filled with something derived would be a fact nobody established --
    # storing a wrong value is worse than storing none. It arrives when the
    # scope carries a tenant, in the change that needs it.
    Column("last_sequence", BigInteger, nullable=False, server_default="0"),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)

events = Table(
    "events",
    metadata,
    Column("event_id", String(IDENTIFIER_LENGTH), primary_key=True),
    Column("stream_id", String(IDENTIFIER_LENGTH), nullable=False),
    Column("run_id", String(IDENTIFIER_LENGTH), nullable=False),
    Column("sequence", BigInteger, nullable=False),
    # Optional because most observational events do not need idempotency. When
    # present, the key identifies one durable append within this stream.
    Column("event_key", String(IDENTIFIER_LENGTH), nullable=True),
    # Stored beside the payload rather than inferred from its shape. Replay
    # must know which envelope contract produced a row before it attempts to
    # interpret that row.
    Column("schema_version", Integer, nullable=False),
    Column("event_type", String(64), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("task_id", String(IDENTIFIER_LENGTH), nullable=True),
    Column("graph_node_id", String(IDENTIFIER_LENGTH), nullable=True),
    Column("parent_event_id", String(IDENTIFIER_LENGTH), nullable=True),
    Column(
        "recorded_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    # If the stream lock is ever bypassed, the write fails instead of quietly
    # reusing a position two subscribers would resume from differently.
    UniqueConstraint("stream_id", "sequence", name="uq_events_stream_sequence"),
    # The replay query: one stream, everything after a cursor, in order.
    Index("ix_events_stream_sequence", "stream_id", "sequence"),
    Index(
        "uq_events_stream_event_key",
        "stream_id",
        "event_key",
        unique=True,
        postgresql_where=text("event_key IS NOT NULL"),
    ),
)
