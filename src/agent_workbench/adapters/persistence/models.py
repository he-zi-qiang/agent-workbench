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
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

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
DIGEST_LENGTH = 64
FILENAME_LENGTH = 255
# Mirrors ports.task_registry.OBJECTIVE_PREVIEW_LIMIT. The port bounds what may
# be constructed; this bounds what may be stored, and a test asserts they agree
# so a widened preview cannot start silently failing inserts.
OBJECTIVE_PREVIEW_LENGTH = 200

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

chat_turns = Table(
    "chat_turns",
    metadata,
    Column("turn_id", String(IDENTIFIER_LENGTH), primary_key=True),
    Column(
        "session_id",
        String(IDENTIFIER_LENGTH),
        ForeignKey("conversation_sessions.session_id", ondelete="CASCADE"),
        nullable=False,
    ),
    # Idempotency is scoped to the conversation. The caller may reuse the same
    # transport key in another session without linking the two conversations.
    Column("idempotency_key", String(IDENTIFIER_LENGTH), nullable=False),
    Column("request_hash", String(DIGEST_LENGTH), nullable=False),
    # A run is a globally addressable trace, so it cannot back two turns.
    Column("run_id", String(IDENTIFIER_LENGTH), nullable=False, unique=True),
    Column("status", String(32), nullable=False),
    # Fixed execution deadline. It is present only while the model execution
    # owns the Turn; moving to pending or any terminal state clears it.
    Column("lease_until", DateTime(timezone=True), nullable=True),
    Column(
        "user_message_id",
        String(IDENTIFIER_LENGTH),
        ForeignKey("messages.message_id"),
        nullable=False,
    ),
    Column(
        "assistant_message_id",
        String(IDENTIFIER_LENGTH),
        ForeignKey("messages.message_id"),
        nullable=True,
    ),
    # These are complete versioned Pydantic aggregates. Repositories validate
    # them on every read instead of treating JSONB as an untyped cache.
    #
    # none_as_null is not decoration. JSONB has two distinguishable emptinesses
    # -- SQL NULL and the JSON value null -- and SQLAlchemy writes Python None
    # as the latter by default. The lifecycle constraint below is phrased in
    # SQL NULL, so without this a turn that has no failure stores json 'null',
    # which IS NOT NULL, and every non-failed transition is rejected. These are
    # the only nullable JSONB columns in the schema, which is why nothing
    # before them needed to say this.
    Column("result", JSONB(none_as_null=True), nullable=True),
    Column("failure_outcome", JSONB(none_as_null=True), nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    UniqueConstraint(
        "session_id",
        "idempotency_key",
        name="uq_chat_turns_session_id_idempotency_key",
    ),
    CheckConstraint(
        "status IN "
        "('running', 'release_pending', 'committed', 'withheld', "
        "'failed', 'cancelled')",
        name="chat_turns_status",
    ),
    CheckConstraint(
        "(status = 'running' AND lease_until IS NOT NULL) OR "
        "(status <> 'running' AND lease_until IS NULL)",
        name="chat_turns_lease",
    ),
    CheckConstraint(
        "("
        "status = 'running' AND assistant_message_id IS NULL "
        "AND result IS NULL AND failure_outcome IS NULL"
        ") OR ("
        "status = 'release_pending' AND assistant_message_id IS NULL "
        "AND result IS NOT NULL AND failure_outcome IS NULL"
        ") OR ("
        "status IN ('committed', 'withheld') "
        "AND assistant_message_id IS NOT NULL "
        "AND result IS NOT NULL AND failure_outcome IS NULL"
        ") OR ("
        "status IN ('failed', 'cancelled') AND assistant_message_id IS NULL "
        "AND result IS NULL AND failure_outcome IS NOT NULL"
        ")",
        name="chat_turns_lifecycle",
    ),
    # PostgreSQL enforces the same non-interleaving invariant as the session
    # row lock. The index is the final guard if a future writer bypasses this
    # repository.
    Index(
        "uq_chat_turns_active_session",
        "session_id",
        unique=True,
        postgresql_where=text("status IN ('running', 'release_pending')"),
    ),
    Index(
        "ix_chat_turns_expired_running",
        "lease_until",
        "turn_id",
        postgresql_where=text("status = 'running'"),
    ),
)

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

knowledge_bases = Table(
    "knowledge_bases",
    metadata,
    # Knowledge-base ids are generated globally, but tenant remains part of the
    # primary key so every lookup is structurally tenant-scoped rather than
    # relying on UUID probability as an authorization boundary.
    Column("knowledge_base_id", String(IDENTIFIER_LENGTH), primary_key=True),
    Column("tenant_id", String(IDENTIFIER_LENGTH), primary_key=True),
    Column("owner_id", String(IDENTIFIER_LENGTH), nullable=False),
    Column("name", String(200), nullable=False),
    Column("description", Text, nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Index(
        "ix_knowledge_bases_tenant_id_owner_id_created_at",
        "tenant_id",
        "owner_id",
        "created_at",
    ),
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
    # The revision the last ingestion attempt refused, and why. Recorded
    # because "not indexed yet" and "will never be indexed" were the same
    # observable state: the outbox retries a poison document forever, and the
    # only thing anybody could see was a revision that stayed behind.
    # Revision-scoped rather than a flag, so a re-upload is not born failed.
    Column("failed_revision", BigInteger, nullable=True),
    # An ErrorCode, never a parser's message: that text quotes the document's
    # own bytes, and this column is read by every principal who can read the
    # knowledge base.
    Column("failure_code", String(32), nullable=True),
    Column("deleted", Boolean, nullable=False, server_default=text("false")),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    CheckConstraint(
        # Half a failure is not representable: a revision with no code says
        # nothing, and a code with no revision belongs to no revision.
        "(failed_revision IS NULL) = (failure_code IS NULL)",
        name="documents_failure_is_whole",
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
        "kind IN ('document_upserted', 'document_deleted', 'acl_changed', "
        "'graph_extraction_requested')",
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


# --------------------------------------------------------------------------
# The retrieval graph (ADR-037)
#
# Entities merge inside one knowledge base so that two documents naming the
# same thing become one way in. Evidence does not merge: every entity and
# every relationship keeps per-mention rows pointing at the chunk it was read
# from, and retrieval nominates *those chunks*. That is the whole difference
# from a merged knowledge graph -- a node built out of two documents cannot
# answer "may this principal read it", and the ACL re-check is by document.


kg_entities = Table(
    "kg_entities",
    metadata,
    Column("entity_id", String(IDENTIFIER_LENGTH), primary_key=True),
    Column("tenant_id", String(IDENTIFIER_LENGTH), nullable=False),
    Column("knowledge_base_id", String(IDENTIFIER_LENGTH), nullable=False),
    # The merge key. Normalised at write time so "Team Marlin" and "team
    # marlin" are one way in rather than two.
    Column("normalized_name", String(512), nullable=False),
    Column("entity_type", String(64), nullable=False),
    # What the reader sees, from the first mention that created the row.
    Column("display_name", String(512), nullable=False),
    # Extraction model + prompt version + embedder identity. An entity written
    # under a different identity is not comparable with these and is not
    # nominated beside them (ADR-037 §2.5).
    Column("graph_identity", String(256), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    # Merging is per knowledge base, never per tenant: two knowledge bases are
    # two corpora, and an entity common to both must not make one nominate the
    # other's chunks.
    UniqueConstraint(
        "tenant_id",
        "knowledge_base_id",
        "normalized_name",
        "entity_type",
        "graph_identity",
        name="uq_kg_entities_merge_key",
    ),
)

kg_mentions = Table(
    "kg_mentions",
    metadata,
    Column("mention_id", String(IDENTIFIER_LENGTH), primary_key=True),
    Column(
        "entity_id",
        String(IDENTIFIER_LENGTH),
        ForeignKey("kg_entities.entity_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("tenant_id", String(IDENTIFIER_LENGTH), nullable=False),
    Column("knowledge_base_id", String(IDENTIFIER_LENGTH), nullable=False),
    # The provenance that makes authorization possible. No foreign key to
    # documents, for the reason outbox_events gives: this row has to be
    # deletable by the same path that forgets the document.
    Column("document_id", String(IDENTIFIER_LENGTH), nullable=False),
    Column("document_version", String(IDENTIFIER_LENGTH), nullable=False),
    Column("chunk_id", String(IDENTIFIER_LENGTH), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    # One mention per (entity, chunk): a chunk naming an entity twice is still
    # one nomination, and re-extracting the same version must not accumulate.
    UniqueConstraint("entity_id", "chunk_id", name="uq_kg_mentions_entity_chunk"),
    # Nomination reads by entity; forgetting a document reads by document.
    Index("ix_kg_mentions_entity_id", "entity_id"),
    Index("ix_kg_mentions_document", "tenant_id", "document_id"),
    # Seed expansion reads this way round: given the chunks the other arms
    # already found, which entities did they name (ADR-037 §2.7). Without it
    # the hot path scans, and the arm's whole budget is two index lookups.
    Index("ix_kg_mentions_chunk", "tenant_id", "knowledge_base_id", "chunk_id"),
)

kg_relations = Table(
    "kg_relations",
    metadata,
    Column("relation_id", String(IDENTIFIER_LENGTH), primary_key=True),
    Column("tenant_id", String(IDENTIFIER_LENGTH), nullable=False),
    Column("knowledge_base_id", String(IDENTIFIER_LENGTH), nullable=False),
    Column(
        "subject_entity_id",
        String(IDENTIFIER_LENGTH),
        ForeignKey("kg_entities.entity_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "object_entity_id",
        String(IDENTIFIER_LENGTH),
        ForeignKey("kg_entities.entity_id", ondelete="CASCADE"),
        nullable=False,
    ),
    # What the edge says, in the extractor's words. This is what the relation
    # arm embeds, so it is the text a query is matched against.
    Column("description", String(2048), nullable=False),
    # Same provenance as a mention, and for the same reason: a relation
    # nominates the chunk it was read from, never a chunk it was inferred over.
    Column("document_id", String(IDENTIFIER_LENGTH), nullable=False),
    Column("document_version", String(IDENTIFIER_LENGTH), nullable=False),
    Column("chunk_id", String(IDENTIFIER_LENGTH), nullable=False),
    Column("graph_identity", String(256), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    UniqueConstraint(
        "subject_entity_id",
        "object_entity_id",
        "chunk_id",
        name="uq_kg_relations_edge_chunk",
    ),
    Index("ix_kg_relations_document", "tenant_id", "document_id"),
)


__all__ = [
    "DIGEST_LENGTH",
    "FILENAME_LENGTH",
    "IDENTIFIER_LENGTH",
    "NAMING_CONVENTION",
    "artifacts",
    "chat_turns",
    "conversation_sessions",
    "document_acl",
    "document_versions",
    "documents",
    "kg_entities",
    "kg_mentions",
    "kg_relations",
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


# Where a graph's execution position lives. ADR-014 chose to implement
# LangGraph's BaseCheckpointSaver against this stack rather than install the
# official saver, so these tables are this project's Alembic chain rather than
# a second migration history.
#
# The decomposition is not a design choice: it is what the contract asks for.
# `aput` receives `new_versions`, the channels this write actually changed, so
# channel values belong in a table keyed by version rather than copied into
# every checkpoint; `aput_writes` records a task's output before the step that
# consumes it is checkpointed, so it needs a table of its own.
#
# Everything LangGraph serialises stays opaque here. `serde.dumps_typed` returns
# a (type, bytes) pair and `loads_typed` takes the same pair back, so both halves
# are stored and neither is interpreted. Widening the columns into something
# readable would mean this project claiming to understand a format it does not
# own, and getting it wrong precisely when a checkpoint has to be recovered.
#
# The names carry a `workflow_` prefix because the ecosystem's unprefixed
# `checkpoints` is a table the official saver creates with `IF NOT EXISTS` and
# a different column layout. Nothing here should be silently adopted by it.

workflow_checkpoints = Table(
    "workflow_checkpoints",
    metadata,
    # This project's Identifier, so it is bounded like every other one.
    Column("thread_id", String(IDENTIFIER_LENGTH), primary_key=True),
    # The remaining identifiers are minted by LangGraph, not by us. Bounding a
    # value another library generates buys nothing and fails a legitimate run:
    # `checkpoint_ns` grows with subgraph nesting and has no documented limit.
    # It is the empty string for a flat graph, which is a value, not a default.
    Column("checkpoint_ns", Text, primary_key=True),
    Column("checkpoint_id", Text, primary_key=True),
    # Null exactly at the root of a thread. The chain is what `parent_config`
    # is read from, and what a fork walks back through.
    Column("parent_checkpoint_id", Text, nullable=True),
    Column("payload_type", Text, nullable=False),
    Column("payload", LargeBinary, nullable=False),
    # The one part that is not opaque. `alist(filter=...)` queries metadata by
    # key, which bytes cannot answer; the contract documents this as a mapping
    # of JSON scalars, and its own `get_checkpoint_metadata` strips NUL from
    # strings -- the single thing that would make JSONB reject the row.
    Column("metadata", JSONB, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    # No index beyond the primary key. Reading a thread's latest checkpoint is
    # a descending scan of the key's third column under a fixed prefix, listing
    # a thread's history is that same prefix, and deleting a thread is its
    # first column. A metadata filter applies within one thread, whose history
    # is bounded by the graph's steps.
)

workflow_checkpoint_blobs = Table(
    "workflow_checkpoint_blobs",
    metadata,
    Column("thread_id", String(IDENTIFIER_LENGTH), primary_key=True),
    Column("checkpoint_ns", Text, primary_key=True),
    Column("channel", Text, primary_key=True),
    # `ChannelVersions` allows str, int or float. Text is the only type that
    # holds all three without deciding which one LangGraph is entitled to send.
    Column("version", Text, primary_key=True),
    # A channel that carried no value at this version is recorded, not omitted:
    # the type says so and the payload is empty. Leaving the row out instead
    # would make "never written" and "written as nothing" the same absence.
    Column("payload_type", Text, nullable=False),
    Column("payload", LargeBinary, nullable=False),
    # A blob is reachable only through the `channel_versions` map inside a
    # checkpoint's opaque payload, so no SQL join can date it from the
    # checkpoints that reference it. Without its own timestamp the only
    # possible retention is per-thread.
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)

workflow_checkpoint_writes = Table(
    "workflow_checkpoint_writes",
    metadata,
    Column("thread_id", String(IDENTIFIER_LENGTH), primary_key=True),
    Column("checkpoint_ns", Text, primary_key=True),
    Column("checkpoint_id", Text, primary_key=True),
    Column("task_id", Text, primary_key=True),
    # Signed on purpose. Ordinary writes take their position in the batch;
    # WRITES_IDX_MAP gives errors, interrupts and resumes negative positions so
    # they cannot collide with a write that merely happened to be first.
    Column("idx", Integer, primary_key=True),
    Column("channel", Text, nullable=False),
    Column("task_path", Text, nullable=False),
    Column("payload_type", Text, nullable=False),
    Column("payload", LargeBinary, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    # Deliberately no foreign key to workflow_checkpoints. Under LangGraph's
    # default `durability="async"` the checkpoint put is not awaited before the
    # next step's writes are issued, so a write routinely reaches the database
    # before the row it names -- measured across every durability mode, a
    # failing node and a resume. A foreign key here would not enforce an
    # invariant; it would fail ordinary runs. See tests/persistence.
)


# What a human was asked, and what they answered.
#
# A decision is a fact with a version, not an event to be replayed: the same
# decision arriving twice -- a retried request, a double-clicked button -- must
# leave one row and requeue the Task once. That is what `decision_version`
# carries, and why a decision is stored beside the Task rather than derived from
# the event stream.

approvals = Table(
    "approvals",
    metadata,
    Column("approval_id", String(IDENTIFIER_LENGTH), primary_key=True),
    Column(
        "task_id",
        String(IDENTIFIER_LENGTH),
        ForeignKey("task_runs.task_id"),
        nullable=False,
    ),
    # Which interrupt inside the graph this answers. Unique per Task, so one
    # node's pause cannot accumulate two competing approvals.
    Column("graph_node_operation_id", String(IDENTIFIER_LENGTH), nullable=False),
    Column("tenant_id", String(IDENTIFIER_LENGTH), nullable=False),
    Column("owner_id", String(IDENTIFIER_LENGTH), nullable=False),
    Column("status", String(16), nullable=False),
    # Monotonic per approval. A decision that arrives with a version already
    # recorded is the same decision again; a later one supersedes.
    Column("decision_version", Integer, nullable=False, server_default="0"),
    Column("decided_by", String(IDENTIFIER_LENGTH), nullable=True),
    Column("decided_at", DateTime(timezone=True), nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    CheckConstraint(
        "status IN ('pending', 'approved', 'rejected')",
        name="approvals_status",
    ),
    # A pending approval has nobody attached to it; a decided one names who and
    # when. Left one-way, a stale decider would outlive the decision it made.
    CheckConstraint(
        "(status = 'pending' AND decision_version = 0 "
        "AND decided_by IS NULL AND decided_at IS NULL) OR "
        "(status <> 'pending' AND decision_version >= 1 "
        "AND decided_by IS NOT NULL AND decided_at IS NOT NULL)",
        name="approvals_decision",
    ),
    UniqueConstraint(
        "task_id",
        "graph_node_operation_id",
        name="uq_approvals_task_id_graph_node_operation_id",
    ),
    Index("ix_approvals_task_id", "task_id"),
)


# What was attempted outside this database, and what came back.
#
# The row exists before the effect does. That ordering is the whole design: an
# external call cannot join this transaction, so the only thing that can be made
# durable at the right moment is the *intent*. A process that dies between the
# dispatch and the report leaves a row saying an effect was intended and not
# saying whether it landed -- which is a fact, and a more useful one than either
# guess.
#
# `operation_key` is a stable business key and is unique per Task. A model's
# `tool_call_id` is recorded beside it but is deliberately not part of it: a
# retried turn mints a new call id for the same intent, so keying on it would
# make every retry a fresh operation and every retry a second effect.

tool_executions = Table(
    "tool_executions",
    metadata,
    Column("execution_id", String(IDENTIFIER_LENGTH), primary_key=True),
    Column(
        "task_id",
        String(IDENTIFIER_LENGTH),
        ForeignKey("task_runs.task_id"),
        nullable=False,
    ),
    Column("operation_key", String(IDENTIFIER_LENGTH), nullable=False),
    Column("tool_name", String(128), nullable=False),
    # The digest of the canonical arguments, never the arguments: this table is
    # read by operators, and tool arguments carry user text and retrieved
    # passages.
    Column("canonical_request_hash", String(64), nullable=False),
    Column("status", String(24), nullable=False),
    # The claim that recorded the intent. Every later write to this row must
    # match it, so a Worker that lost the Task cannot report a result for an
    # effect the new one is now responsible for.
    Column("lease_epoch", Integer, nullable=False),
    Column("agent_run_id", String(IDENTIFIER_LENGTH), nullable=False),
    Column("tool_call_id", String(IDENTIFIER_LENGTH), nullable=False),
    # Revision and canonical fingerprint together, so a rule set that kept its
    # label while its rules changed is still distinguishable after the fact.
    Column("policy_identity", String(256), nullable=False),
    Column("outcome_detail", String(256), nullable=True),
    Column(
        "intended_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column("settled_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "status IN ('intended', 'succeeded', 'failed', 'needs_reconciliation')",
        name="tool_executions_status",
    ),
    # An unsettled operation has no settlement time and a settled one does.
    # Without both directions, a crashed attempt could be read as an old
    # success whose timestamp was simply never written.
    CheckConstraint(
        "(status = 'intended' AND settled_at IS NULL) OR "
        "(status <> 'intended' AND settled_at IS NOT NULL)",
        name="tool_executions_settlement",
    ),
    # A claim's epoch starts at 1. Zero would be a row written by nothing.
    CheckConstraint("lease_epoch >= 1", name="tool_executions_lease_epoch"),
    UniqueConstraint(
        "task_id",
        "operation_key",
        name="uq_tool_executions_task_id_operation_key",
    ),
    Index("ix_tool_executions_task_id", "task_id"),
    # How an operator finds the rows that need them.
    Index("ix_tool_executions_status", "status"),
)


# Which concrete Qdrant index a Task may be bound to.
#
# The alias is not in here on purpose. An alias selects an index for *new*
# requests; it is not recoverable semantics, because the thing it points at can
# move while a Task is mid-run. What a Task stores is the generation it was
# reserved against, and the foreign key from task_runs *is* that reservation:
# while any Task still references a generation, the row cannot be deleted, so
# neither can the collection it names.
#
# This is the minimum a reservation needs. The rest of a generation's life --
# backfill progress, index_ready, retention windows -- belongs to the ingestion
# state the plan assigns to WP04-05, and is deliberately absent rather than
# guessed at here.

qdrant_index_generations = Table(
    "qdrant_index_generations",
    metadata,
    Column("generation_id", UUID(as_uuid=False), primary_key=True),
    Column("collection_name", String(IDENTIFIER_LENGTH), nullable=False),
    Column("index_version", String(64), nullable=False),
    # Only `active` may be newly reserved. `draining` keeps existing
    # reservations valid while refusing new ones, which is what lets an alias
    # switch drain instead of cutting; `retired` may be deleted once nothing
    # references it.
    Column("status", String(16), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    CheckConstraint(
        "status IN ('active', 'draining', 'retired')",
        name="qdrant_index_generations_status",
    ),
    # One generation per (collection, version): the pair is what a Task's
    # snapshot records, so two rows for it would make the snapshot ambiguous.
    UniqueConstraint(
        "collection_name",
        "index_version",
        name="uq_qdrant_index_generations_collection_name_index_version",
    ),
    # The resolver's query: the one generation currently taking reservations.
    Index(
        "uq_qdrant_index_generations_active",
        "collection_name",
        unique=True,
        postgresql_where=text("status = 'active'"),
    ),
)


# The Task Registry: product lifecycle, as opposed to the execution position
# the checkpoint tables above hold. The two are separate facts in separate
# places on purpose, and the Worker's reconciliation decides what they jointly
# mean rather than pretending they commit together.
#
# The lease, epoch, attempt counter and ``available_at`` backoff below are E1
# coordination data.  They fence Registry lifecycle writes but not yet
# LangGraph checkpoint writes; the saver-level epoch predicate is E2.  The
# run-semantics snapshot and submitted policy identity are WP07-03; resolved
# Qdrant collection, index version and generation reservation are WP07-04.

task_runs = Table(
    "task_runs",
    metadata,
    Column("task_id", String(IDENTIFIER_LENGTH), primary_key=True),
    Column("tenant_id", String(IDENTIFIER_LENGTH), nullable=False),
    Column("owner_id", String(IDENTIFIER_LENGTH), nullable=False),
    # The workflow thread this Task's execution position lives under. Unique
    # in both directions: a Task addresses exactly one thread, and a thread
    # backs exactly one Task, so no reconciliation can ever be handed two
    # Registry rows for one checkpoint.
    Column("thread_id", String(IDENTIFIER_LENGTH), nullable=False, unique=True),
    # What this Task was submitted to run. The checkpoint separately records
    # what actually wrote its position; the two disagreeing is the entire
    # migration case, so neither may be derived from the other.
    Column("graph_version", String(64), nullable=False),
    # Where the submitted input is stored. Large inputs do not live in this
    # row, and a Task with no checkpoint is started from this reference.
    Column("input_ref", String(IDENTIFIER_LENGTH), nullable=False),
    # The canonical content identity, used for idempotency instead of the
    # generated artifact reference. A retry may leave an orphan equal-input
    # artifact, but it must return the Task opened by the first writer.
    Column("input_fingerprint", String(DIGEST_LENGTH), nullable=False),
    Column("submission_dedup_key", String(IDENTIFIER_LENGTH), nullable=False),
    # A label so a list of Tasks reads as work rather than as identifiers. Not
    # the objective: that stays in the input artifact, and this is a bounded
    # copy taken once at submission. Nullable because rows written before this
    # column existed have no label, and inventing one from the id would be
    # worse than showing the id.
    Column("objective_preview", String(OBJECTIVE_PREVIEW_LENGTH), nullable=True),
    # What this Task means, resolved at submission and never re-resolved.
    # Deterministic semantics only: the settings layer builds it, and it
    # excludes alias, policy, DSN, secret, endpoint and coordination -- a
    # resume restores what the Task meant, not where the deployment was.
    Column("run_semantics_snapshot", JSONB, nullable=False),
    Column("run_semantics_revision", String(128), nullable=False),
    # Policy identity is stored beside the snapshot rather than inside it,
    # because policy is re-evaluated on every claim and every dispatch. These
    # two record which rules the caller was granted under; the effective
    # authorization is always that envelope intersected with current policy.
    Column("submitted_policy_revision", String(128), nullable=False),
    Column("submitted_policy_fingerprint", String(DIGEST_LENGTH), nullable=False),
    Column("submitted_authorization_envelope", JSONB, nullable=False),
    Column("submitted_principal_scopes", JSONB, nullable=False),
    # The concrete index this Task was reserved against, resolved once at
    # submission. All three or none: a Task that uses a knowledge base carries
    # the full triple, and one that does not carries nothing. Half of it would
    # be a snapshot nobody can act on.
    Column("resolved_qdrant_collection", String(IDENTIFIER_LENGTH), nullable=True),
    Column("resolved_qdrant_index_version", String(64), nullable=True),
    Column(
        "resolved_qdrant_index_generation_id",
        UUID(as_uuid=False),
        # The reservation itself, and not a cache of Qdrant's routing state:
        # while this row exists the generation cannot be deleted.
        ForeignKey("qdrant_index_generations.generation_id"),
        nullable=True,
    ),
    Column("status", String(32), nullable=False),
    # A claim is deliberately separate from the Task's product status. The
    # epoch is monotonic across claims; an old Worker can therefore never
    # complete a Task after a reclaimed Worker has moved it forward.
    Column("lease_owner", String(IDENTIFIER_LENGTH), nullable=True),
    Column("lease_epoch", BigInteger, nullable=False, server_default=text("0")),
    Column("lease_until", DateTime(timezone=True), nullable=True),
    Column("heartbeat_at", DateTime(timezone=True), nullable=True),
    Column("attempt_count", Integer, nullable=False, server_default=text("0")),
    # How many agent invocations this Task has paid for, across every retry
    # and every reclaim (ADR-040). Distinct from `attempt_count`, which
    # counts how many times the Task was *claimed*: one claim can run many
    # agent nodes, and a Task reclaimed after a crash keeps what it spent.
    Column("agent_invocation_count", Integer, nullable=False, server_default=text("0")),
    Column(
        "available_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    # Why a Task stopped where it did. Required exactly for the states a human
    # has to act on or account for, so "failed" can never be recorded without
    # saying what failed, and a Task parked for a migration always carries the
    # reconciliation's own sentence.
    # Why this Task is back on the queue, when it is not simply new work. Set
    # together with the approval that caused it, so a Worker resuming can tell
    # a retry from a decision without inspecting the graph.
    Column("resume_kind", String(16), nullable=True),
    Column("resume_approval_id", String(IDENTIFIER_LENGTH), nullable=True),
    Column("status_detail", Text, nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    # Resubmitting the same key returns the same Task rather than starting a
    # second one. It is scoped to the tenant *and* owner: principal ids are
    # only meaningful inside a tenant, so omitting tenant_id would let one
    # tenant's retry deny another tenant's submission.
    UniqueConstraint(
        "tenant_id",
        "owner_id",
        "submission_dedup_key",
        name="uq_task_runs_tenant_id_owner_id_submission_dedup_key",
    ),
    CheckConstraint(
        "status IN "
        "('queued', 'running', 'waiting_approval', 'waiting_migration', "
        "'succeeded', 'failed', 'cancelled', 'dead_letter')",
        name="task_runs_status",
    ),
    CheckConstraint(
        "(resolved_qdrant_collection IS NULL "
        "AND resolved_qdrant_index_version IS NULL "
        "AND resolved_qdrant_index_generation_id IS NULL) OR "
        "(resolved_qdrant_collection IS NOT NULL "
        "AND resolved_qdrant_index_version IS NOT NULL "
        "AND resolved_qdrant_index_generation_id IS NOT NULL)",
        name="task_runs_resolved_index",
    ),
    CheckConstraint(
        "(resume_kind IS NULL AND resume_approval_id IS NULL) OR "
        "(resume_kind = 'approval' AND resume_approval_id IS NOT NULL)",
        name="task_runs_resume_reference",
    ),
    CheckConstraint(
        "(resolved_qdrant_collection IS NULL "
        "AND resolved_qdrant_index_version IS NULL "
        "AND resolved_qdrant_index_generation_id IS NULL) OR "
        "(resolved_qdrant_collection IS NOT NULL "
        "AND resolved_qdrant_index_version IS NOT NULL "
        "AND resolved_qdrant_index_generation_id IS NOT NULL)",
        name="task_runs_resolved_index",
    ),
    CheckConstraint(
        "(resume_kind IS NULL AND resume_approval_id IS NULL) OR "
        "(resume_kind = 'approval' AND resume_approval_id IS NOT NULL)",
        name="task_runs_resume_reference",
    ),
    CheckConstraint(
        "(status IN ('waiting_migration', 'failed', 'cancelled', 'dead_letter') "
        "AND status_detail IS NOT NULL) OR "
        "(status IN ('queued', 'running', 'waiting_approval', 'succeeded') "
        "AND status_detail IS NULL)",
        name="task_runs_status_detail",
    ),
    CheckConstraint(
        "(status = 'running' AND lease_owner IS NOT NULL "
        "AND lease_until IS NOT NULL AND heartbeat_at IS NOT NULL) OR "
        "(status <> 'running' AND lease_owner IS NULL "
        "AND lease_until IS NULL AND heartbeat_at IS NULL)",
        name="task_runs_lease_lifecycle",
    ),
    CheckConstraint(
        "lease_epoch >= 0 AND attempt_count >= 0 AND agent_invocation_count >= 0",
        name="task_runs_lease_counters",
    ),
    # The pick order for a Worker looking for work: oldest queued first. The
    # partial index means that scan never walks the finished ones, which are
    # eventually most of the table.
    Index(
        "ix_task_runs_queued",
        "created_at",
        "task_id",
        postgresql_where=text("status = 'queued'"),
    ),
    Index(
        "ix_task_runs_claim_eligible",
        "available_at",
        "created_at",
        "task_id",
        postgresql_where=text("status = 'queued'"),
    ),
    Index(
        "ix_task_runs_expired_lease",
        "lease_until",
        "task_id",
        postgresql_where=text("status = 'running'"),
    ),
    Index("ix_task_runs_tenant_id_task_id", "tenant_id", "task_id"),
)
