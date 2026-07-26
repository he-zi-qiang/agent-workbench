"""PostgreSQL persistence.

PostgreSQL is the source of truth for conversations, the task registry, run
events, approvals and documents. This package holds the schema those facts live
in and the repositories that read and write them; the vector index is a derived
copy and lives elsewhere.
"""

from agent_workbench.adapters.persistence.conversation_store import (
    PostgresConversationStore,
)
from agent_workbench.adapters.persistence.engine import create_query_engine
from agent_workbench.adapters.persistence.models import metadata

__all__ = ["PostgresConversationStore", "create_query_engine", "metadata"]
