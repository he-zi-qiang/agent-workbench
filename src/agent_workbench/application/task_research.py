"""Produce and re-read Task evidence under the Task owner's identity."""

from __future__ import annotations

from dataclasses import dataclass

from agent_workbench.application.retrieval import RetrievalRequest, RetrievalService
from agent_workbench.domain.artifacts import ArtifactRef
from agent_workbench.domain.evidence import (
    MAX_EVIDENCE_ITEMS,
    EvidenceBundle,
    EvidenceItem,
    EvidenceRevision,
    EvidenceSource,
)
from agent_workbench.domain.identifiers import (
    new_evidence_id,
)
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.ports.artifact_store import ArtifactStore
from agent_workbench.ports.cancellation import CancellationToken
from agent_workbench.ports.conversation_store import AuthorizedRevision
from agent_workbench.ports.research import ExternalSearchPort, SourcesUnreadableError

EVIDENCE_MEDIA_TYPE = "application/json"
EVIDENCE_FILENAME = "evidence-bundle.json"
DEFAULT_INTERNAL_TOP_K = 8
DEFAULT_EXTERNAL_LIMIT = 8


class EvidenceArtifactError(RuntimeError):
    """An owned artifact does not contain the expected evidence bundle."""


class EvidenceUnavailableError(RuntimeError):
    """A research source produced no evidence safe to continue from."""


@dataclass(frozen=True, slots=True)
class TaskResearchContext:
    """Server-owned task and identity facts; none arrive from model arguments."""

    task_id: str
    principal: PrincipalContext
    knowledge_base_id: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceStore:
    """Store bundles as artifacts and reload them under owner/tenant checks."""

    artifacts: ArtifactStore

    async def save(
        self, *, context: TaskResearchContext, bundle: EvidenceBundle
    ) -> ArtifactRef:
        if bundle.task_id != context.task_id:
            raise ValueError("evidence bundle task_id does not match its context")
        return await self.artifacts.put(
            tenant_id=context.principal.tenant_id,
            owner_id=context.principal.principal_id,
            kind="evidence_bundle",
            media_type=EVIDENCE_MEDIA_TYPE,
            content=bundle.model_dump_json().encode("utf-8"),
            filename=EVIDENCE_FILENAME,
        )

    async def load(
        self,
        *,
        context: TaskResearchContext,
        artifact_id: str,
        source: EvidenceSource | None = None,
    ) -> EvidenceBundle:
        reference = await self.artifacts.head(
            tenant_id=context.principal.tenant_id,
            artifact_id=artifact_id,
            principal_id=context.principal.principal_id,
        )
        if (
            reference.kind != "evidence_bundle"
            or reference.media_type != EVIDENCE_MEDIA_TYPE
        ):
            raise EvidenceArtifactError("artifact is not an evidence bundle")
        content = await self.artifacts.get(
            tenant_id=context.principal.tenant_id,
            artifact_id=artifact_id,
            principal_id=context.principal.principal_id,
        )
        try:
            bundle = EvidenceBundle.model_validate_json(content)
        except Exception as error:
            raise EvidenceArtifactError(
                "evidence artifact has an invalid schema"
            ) from error
        if bundle.task_id != context.task_id:
            raise EvidenceArtifactError("evidence artifact belongs to another task")
        if source is not None and bundle.source != source:
            raise EvidenceArtifactError("evidence artifact has an unexpected source")
        return bundle


@dataclass(frozen=True, slots=True)
class InternalResearchService:
    retrieval: RetrievalService
    evidence: EvidenceStore
    top_k: int = DEFAULT_INTERNAL_TOP_K

    def __post_init__(self) -> None:
        if not 1 <= self.top_k <= MAX_EVIDENCE_ITEMS:
            raise ValueError(
                f"top_k must be within 1..{MAX_EVIDENCE_ITEMS} for evidence bounds"
            )

    async def gather(self, *, context: TaskResearchContext, query: str) -> ArtifactRef:
        if context.knowledge_base_id is None:
            raise EvidenceUnavailableError(
                "internal research requires a knowledge base"
            )
        retrieved = await self.retrieval.retrieve(
            RetrievalRequest(
                query=query,
                tenant_id=context.principal.tenant_id,
                principal_id=context.principal.principal_id,
                knowledge_base_id=context.knowledge_base_id,
                top_k=self.top_k,
            )
        )
        citations = {
            citation.chunk_id: citation for citation in retrieved.packet.citations
        }
        try:
            items = tuple(
                EvidenceItem(
                    evidence_id=chunk.chunk_id,
                    source="internal",
                    text=chunk.text,
                    citation=citations[chunk.chunk_id],
                )
                for chunk in retrieved.packet.chunks
            )
        except KeyError as error:
            raise EvidenceUnavailableError(
                "internal research result contains a chunk without a citation"
            ) from error
        if not items:
            raise EvidenceUnavailableError(
                "internal research found no readable evidence"
            )
        bundle = EvidenceBundle(
            task_id=context.task_id,
            source="internal",
            items=items,
            internal_authorized_revisions=tuple(
                EvidenceRevision(document_id=document_id, source_revision=revision)
                for document_id, revision in retrieved.authorized_revisions
            ),
        )
        return await self.evidence.save(context=context, bundle=bundle)

    async def confirm_current(
        self, *, context: TaskResearchContext, bundle: EvidenceBundle
    ) -> bool:
        """Re-check internal ACL/revisions before synthesis output is released."""

        if bundle.source != "internal":
            raise ValueError("only internal evidence has revisions to confirm")
        revisions = tuple(
            AuthorizedRevision(
                document_id=item.document_id,
                source_revision=item.source_revision,
            )
            for item in bundle.internal_authorized_revisions
        )
        return await self.retrieval.revisions_unchanged(
            revisions,
            tenant_id=context.principal.tenant_id,
            principal_id=context.principal.principal_id,
        )


@dataclass(frozen=True, slots=True)
class ExternalResearchService:
    search: ExternalSearchPort
    evidence: EvidenceStore
    limit: int = DEFAULT_EXTERNAL_LIMIT

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= MAX_EVIDENCE_ITEMS:
            raise ValueError(
                f"limit must be within 1..{MAX_EVIDENCE_ITEMS} for evidence bounds"
            )

    async def gather(
        self,
        *,
        context: TaskResearchContext,
        query: str,
        cancellation: CancellationToken,
    ) -> ArtifactRef:
        if not query.strip() or len(query) > 4096:
            raise EvidenceUnavailableError("external-search query is outside its bound")
        cancellation.raise_if_cancelled()
        try:
            hits = await self.search.search(
                query=query,
                limit=self.limit,
                cancellation=cancellation,
            )
        except SourcesUnreadableError as error:
            # Both end this node without evidence, and they are still not the
            # same failure: "no evidence" sends whoever reads the run looking
            # for a better objective, while this one points at the network path
            # out of this deployment. Same outcome, different thing to fix.
            raise EvidenceUnavailableError(
                f"external search found {error.named} page(s) and none could be "
                f"read from this deployment ({error})"
            ) from error
        cancellation.raise_if_cancelled()
        if not hits:
            raise EvidenceUnavailableError("external search returned no evidence")
        if len(hits) > self.limit or len(hits) > MAX_EVIDENCE_ITEMS:
            raise EvidenceUnavailableError("external search exceeded its result limit")
        bundle = EvidenceBundle(
            task_id=context.task_id,
            source="external",
            items=tuple(
                EvidenceItem(
                    evidence_id=new_evidence_id(),
                    source="external",
                    text=hit.text,
                    url=hit.url,
                    title=hit.title,
                )
                for hit in hits
            ),
        )
        return await self.evidence.save(context=context, bundle=bundle)


__all__ = [
    "DEFAULT_EXTERNAL_LIMIT",
    "DEFAULT_INTERNAL_TOP_K",
    "EVIDENCE_FILENAME",
    "EVIDENCE_MEDIA_TYPE",
    "EvidenceArtifactError",
    "EvidenceStore",
    "EvidenceUnavailableError",
    "ExternalResearchService",
    "InternalResearchService",
    "TaskResearchContext",
]
