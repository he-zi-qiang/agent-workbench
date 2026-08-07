from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from agent_workbench.application.knowledge_bases import KnowledgeBaseService
from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.ports.knowledge_bases import (
    KnowledgeBaseRecord,
    KnowledgeBaseSummary,
    KnowledgeDocument,
)

NOW = datetime(2026, 8, 3, tzinfo=UTC)
OWNER = PrincipalContext(tenant_id="tenant_a", principal_id="user_1")
NEIGHBOUR = PrincipalContext(tenant_id="tenant_a", principal_id="user_2")
OTHER_TENANT = PrincipalContext(tenant_id="tenant_b", principal_id="user_1")


class MemoryKnowledgeBases:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], KnowledgeBaseRecord] = {}
        self.documents_by_base: dict[
            tuple[str, str], tuple[KnowledgeDocument, ...]
        ] = {}
        self.readers: dict[tuple[str, str], set[str]] = {}

    async def create(self, record: KnowledgeBaseRecord) -> KnowledgeBaseRecord:
        self.records[(record.tenant_id, record.knowledge_base_id)] = record
        return record

    async def get(
        self, *, tenant_id: str, knowledge_base_id: str
    ) -> KnowledgeBaseRecord | None:
        return self.records.get((tenant_id, knowledge_base_id))

    async def describe_readable(
        self, *, tenant_id: str, principal_id: str, knowledge_base_id: str
    ) -> KnowledgeBaseSummary | None:
        record = await self.get(
            tenant_id=tenant_id, knowledge_base_id=knowledge_base_id
        )
        if record is None or (
            record.owner_id != principal_id
            and principal_id
            not in self.readers.get((tenant_id, knowledge_base_id), set())
        ):
            return None
        documents = self.documents_by_base.get((tenant_id, knowledge_base_id), ())
        ready = sum(document.status == "ready" for document in documents)
        return KnowledgeBaseSummary(
            knowledge_base_id=record.knowledge_base_id,
            name=record.name,
            description=record.description,
            document_count=len(documents),
            ready_document_count=ready,
            processing_document_count=len(documents) - ready,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    async def list_readable(
        self, *, tenant_id: str, principal_id: str
    ) -> tuple[KnowledgeBaseSummary, ...]:
        described = [
            await self.describe_readable(
                tenant_id=tenant_id,
                principal_id=principal_id,
                knowledge_base_id=knowledge_base_id,
            )
            for record_tenant, knowledge_base_id in self.records
            if record_tenant == tenant_id
        ]
        return tuple(record for record in described if record is not None)

    async def list_readable_documents(
        self, *, tenant_id: str, principal_id: str, knowledge_base_id: str
    ) -> tuple[KnowledgeDocument, ...]:
        return self.documents_by_base.get((tenant_id, knowledge_base_id), ())


def test_create_mints_a_kb_id_and_uses_the_shared_projection() -> None:
    store = MemoryKnowledgeBases()
    service = KnowledgeBaseService(store, clock=lambda: NOW)

    created = asyncio.run(
        service.create(OWNER, name="  Handbook  ", description="  Policies  ")
    )

    assert created.knowledge_base_id.startswith("kb_")
    assert created.name == "Handbook"
    assert created.description == "Policies"
    assert created.document_count == 0
    stored = store.records[(OWNER.tenant_id, created.knowledge_base_id)]
    assert stored.owner_id == OWNER.principal_id


def test_get_hides_unknown_other_owner_and_other_tenant_alike() -> None:
    store = MemoryKnowledgeBases()
    service = KnowledgeBaseService(store, clock=lambda: NOW)
    created = asyncio.run(service.create(OWNER, name="Private"))

    for principal in (NEIGHBOUR, OTHER_TENANT):
        with pytest.raises(NotFoundError, match="knowledge base not found"):
            asyncio.run(service.get(principal, created.knowledge_base_id))
    with pytest.raises(NotFoundError, match="knowledge base not found"):
        asyncio.run(service.get(OWNER, "kb_missing"))


def test_write_authority_is_owner_only_and_tenant_scoped() -> None:
    store = MemoryKnowledgeBases()
    service = KnowledgeBaseService(store, clock=lambda: NOW)
    created = asyncio.run(service.create(OWNER, name="Private"))

    allowed = asyncio.run(
        service.require_writable(
            tenant_id=OWNER.tenant_id,
            principal_id=OWNER.principal_id,
            knowledge_base_id=created.knowledge_base_id,
        )
    )
    assert allowed.owner_id == OWNER.principal_id

    for principal in (NEIGHBOUR, OTHER_TENANT):
        with pytest.raises(NotFoundError, match="knowledge base not found"):
            asyncio.run(
                service.require_writable(
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    knowledge_base_id=created.knowledge_base_id,
                )
            )
