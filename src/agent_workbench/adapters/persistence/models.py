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
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    func,
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

# The artifact table arrives with the upload data plane, where its rows have to
# be written in the same transaction as the document version they belong to.
# Adding it here would be schema nothing writes to.

__all__ = [
    "IDENTIFIER_LENGTH",
    "NAMING_CONVENTION",
    "conversation_sessions",
    "messages",
    "metadata",
]
