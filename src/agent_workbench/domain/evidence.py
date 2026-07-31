"""Bounded evidence persisted outside TaskState.

Retrieved and external text is untrusted data, including text that looks like
instructions.  A workflow checkpoint records only the artifact reference; the
evidence bytes, their citations and their authorization revisions live in this
schema-versioned bundle and are read again under the Task owner's identity.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from agent_workbench.domain.context import Citation
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.schema import DomainModel, VersionedModel

EvidenceSource = Literal["internal", "external"]
EvidenceText = Annotated[str, StringConstraints(min_length=1, max_length=8192)]
EvidenceUrl = Annotated[
    str,
    StringConstraints(
        min_length=8,
        max_length=2048,
        pattern=r"^https?://[^\s]+$",
    ),
]
ExternalTitle = Annotated[str, StringConstraints(min_length=1, max_length=512)]

MAX_EVIDENCE_ITEMS = 20


class EvidenceRevision(DomainModel):
    """The exact readable document revision used for internal evidence."""

    document_id: Identifier
    source_revision: int = Field(ge=1)


class EvidenceItem(DomainModel):
    """One bounded passage and the locator that lets a later stage cite it."""

    evidence_id: Identifier
    source: EvidenceSource
    text: EvidenceText
    citation: Citation | None = None
    url: EvidenceUrl | None = None
    title: ExternalTitle | None = None

    @model_validator(mode="after")
    def validate_source_shape(self) -> EvidenceItem:
        if self.source == "internal":
            if self.citation is None or self.url is not None or self.title is not None:
                raise ValueError("internal evidence requires only a citation")
        elif self.citation is not None or self.url is None or self.title is None:
            raise ValueError("external evidence requires a url and title only")
        return self


class EvidenceBundle(VersionedModel):
    """The immutable evidence artifact one research branch produced."""

    task_id: Identifier
    source: EvidenceSource
    items: tuple[EvidenceItem, ...] = Field(min_length=1, max_length=MAX_EVIDENCE_ITEMS)
    internal_authorized_revisions: tuple[EvidenceRevision, ...] = ()

    @model_validator(mode="after")
    def validate_bundle(self) -> EvidenceBundle:
        if len({item.evidence_id for item in self.items}) != len(self.items):
            raise ValueError("evidence ids must be unique")
        if any(item.source != self.source for item in self.items):
            raise ValueError("all evidence items must match the bundle source")
        revisions = self.internal_authorized_revisions
        if self.source == "internal":
            if not revisions:
                raise ValueError("internal evidence requires authorization revisions")
            if len({item.document_id for item in revisions}) != len(revisions):
                raise ValueError(
                    "authorization revisions must have unique document ids"
                )
            cited = {
                item.citation.document_id
                for item in self.items
                if item.citation is not None
            }
            if cited != {item.document_id for item in revisions}:
                raise ValueError("authorization revisions must match cited documents")
        elif revisions:
            raise ValueError("external evidence cannot carry internal revisions")
        return self


class ExternalSearchHit(DomainModel):
    """One provider-neutral, bounded external-search response item."""

    url: EvidenceUrl
    title: ExternalTitle
    text: EvidenceText


__all__ = [
    "MAX_EVIDENCE_ITEMS",
    "EvidenceBundle",
    "EvidenceItem",
    "EvidenceRevision",
    "EvidenceSource",
    "EvidenceText",
    "EvidenceUrl",
    "ExternalSearchHit",
    "ExternalTitle",
]
