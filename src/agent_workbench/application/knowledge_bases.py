"""Create and discover knowledge bases under the caller's identity."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.identifiers import Identifier, new_id
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.ports.knowledge_bases import (
    KnowledgeBaseRecord,
    KnowledgeBaseStore,
    KnowledgeBaseSummary,
    KnowledgeDocument,
)

KNOWLEDGE_BASE_ID_PREFIX = "kb"


@dataclass(frozen=True, slots=True)
class KnowledgeBaseService:
    """Own the identity and authorization rules for knowledge bases."""

    store: KnowledgeBaseStore
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    async def create(
        self,
        principal: PrincipalContext,
        *,
        name: str,
        description: str | None = None,
        knowledge_base_id: str | None = None,
    ) -> KnowledgeBaseSummary:
        """Create an empty knowledge base owned by the authenticated caller."""

        now = self.clock()
        record = KnowledgeBaseRecord(
            knowledge_base_id=knowledge_base_id or new_id(KNOWLEDGE_BASE_ID_PREFIX),
            tenant_id=principal.tenant_id,
            owner_id=principal.principal_id,
            name=name,
            description=description,
            created_at=now,
            updated_at=now,
        )
        await self.store.create(record)
        # Read through the same projection every later request receives.  This
        # prevents create from growing a subtly different response contract.
        described = await self.store.describe_readable(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            knowledge_base_id=record.knowledge_base_id,
        )
        if described is None:  # pragma: no cover - the owner was just persisted
            raise RuntimeError("created knowledge base is not readable by its owner")
        return described

    async def list(
        self, principal: PrincipalContext
    ) -> tuple[KnowledgeBaseSummary, ...]:
        """List only knowledge bases this caller can currently read."""

        return await self.store.list_readable(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
        )

    async def get(
        self, principal: PrincipalContext, knowledge_base_id: Identifier
    ) -> KnowledgeBaseSummary:
        """Return one readable knowledge base, hiding absent and refused alike."""

        described = await self.store.describe_readable(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            knowledge_base_id=knowledge_base_id,
        )
        if described is None:
            raise NotFoundError("knowledge base not found")
        return described

    async def documents(
        self, principal: PrincipalContext, knowledge_base_id: Identifier
    ) -> tuple[KnowledgeDocument, ...]:
        """List readable documents after proving the container is readable."""

        await self.get(principal, knowledge_base_id)
        return await self.store.list_readable_documents(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            knowledge_base_id=knowledge_base_id,
        )

    async def require_writable(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        knowledge_base_id: str,
    ) -> KnowledgeBaseRecord:
        """Require owner-only write authority with non-disclosing refusal."""

        record = await self.store.get(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
        )
        if record is None or record.owner_id != principal_id:
            raise NotFoundError("knowledge base not found")
        return record


__all__ = ["KNOWLEDGE_BASE_ID_PREFIX", "KnowledgeBaseService"]
