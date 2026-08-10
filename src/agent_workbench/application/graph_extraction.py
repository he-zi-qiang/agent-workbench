"""Reading one chunk for the entities and relationships it names (ADR-037).

One structured, toolless model call per chunk. What comes back is a proposal:
the entities become merge keys inside the knowledge base, the relationships
become edges, and every one of them keeps the chunk it was read from so
retrieval can nominate that chunk and authorization can decide by its
document.

Decoding shares ADR-034's boundary through ``workflows.structured_output``:
only a framing failure earns one corrective turn, and an answer that parses
but says something the contract forbids is a claim the model made and got
wrong. Both eventually resolve to *nothing extracted for this chunk* rather
than to an exception, because a chunk the extractor could not read must not
stop the document behind it from being indexed -- the graph is an extra arm,
and a missing arm is a degradation.

The identity is the discipline. Rows carry ``graph_identity`` -- extraction
model, prompt version, embedder identity -- because a graph extracted by a
different model is not the same graph, and nominating from two of them at
once would make a re-extraction silently change what retrieval returns.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from agent_workbench.domain.identifiers import Identifier, new_agent_run_id, new_id
from agent_workbench.domain.knowledge_graph import (
    MAX_ENTITY_NAME,
    MAX_RELATION_DESCRIPTION,
    ChunkExtraction,
    ExtractedEntity,
    ExtractedRelation,
)
from agent_workbench.domain.messages import Message, user_message
from agent_workbench.domain.policies import AuthorizationEnvelope, PrincipalContext
from agent_workbench.domain.runs import AgentRunRequest, RunBudget, TraceContext
from agent_workbench.ports.agent_executor import AgentExecutor
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.event_log import EventSink
from agent_workbench.workflows.structured_output import (
    StructuredOutputError,
    StructuredOutputFramingError,
    json_object,
    restatement_messages,
)

#: Stream prefix for extraction runs. Their events land wherever the caller's
#: sink points and are not a Task timeline: extraction happens during
#: ingestion, which has no Task and no reader waiting.
EXTRACTION_STREAM_PREFIX: Final[str] = "kgx"

#: One completion, no tools. A corrective turn is a second run under the same
#: ceiling rather than a wider first one.
_EXTRACTION_BUDGET: Final[RunBudget] = RunBudget(max_steps=1, max_tool_calls=1)

#: How many entities and relationships one chunk may contribute. A ceiling
#: rather than a target: a chunk that "names" sixty things has produced a list
#: of words, and storing it would make every one of them a nomination.
MAX_ENTITIES_PER_CHUNK: Final[int] = 24
MAX_RELATIONS_PER_CHUNK: Final[int] = 24

_EXTRACTION_CONTRACT: Final[str] = (
    "You read one passage and list what it names. Return exactly one JSON "
    "object and no Markdown, prose, or code fence: "
    '{"entities":[{"name":"...","entity_type":"..."}],'
    '"relations":[{"subject":"...","object":"...","description":"..."}]}. '
    "An entity is something the passage names that another passage could "
    "also name: a component, a team, a store, a code family, an environment. "
    "Do not list generic nouns, and do not list the passage's own topic "
    "unless it is named. entity_type is one lowercase word. "
    "A relation connects two entities the passage *both* names, and its "
    "description is one sentence in the passage's own words saying how they "
    "relate -- not a bare verb. Omit a relation whose endpoints you did not "
    "list as entities. Both lists may be empty when the passage names "
    "nothing worth an entry point."
)


class _EntityDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=MAX_ENTITY_NAME)
    entity_type: str = Field(min_length=1, max_length=64)


class _RelationDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str = Field(min_length=1, max_length=MAX_ENTITY_NAME)
    object: str = Field(min_length=1, max_length=MAX_ENTITY_NAME)
    description: str = Field(min_length=1, max_length=MAX_RELATION_DESCRIPTION)


class _ExtractionDocument(BaseModel):
    """Exactly what the contract permits the model to say."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entities: tuple[_EntityDocument, ...] = Field(
        default=(), max_length=MAX_ENTITIES_PER_CHUNK
    )
    relations: tuple[_RelationDocument, ...] = Field(
        default=(), max_length=MAX_RELATIONS_PER_CHUNK
    )


_DOCUMENT: Final[TypeAdapter[_ExtractionDocument]] = TypeAdapter(_ExtractionDocument)

#: What an unreadable chunk yields. Distinct from "the chunk named nothing"
#: only to the caller, which is told by ``extracted`` -- see ExtractionResult.
_NOTHING: Final[ChunkExtraction] = ChunkExtraction()


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """What one chunk yielded, and whether the extractor got to say so.

    ``extracted`` tells an empty chunk apart from a failed call. Both store
    nothing, but only one of them is a fact about the corpus: a run that
    reported every timeout as "this chunk names nothing" would make a broken
    provider look like a boring document.
    """

    extraction: ChunkExtraction
    extracted: bool


@dataclass(frozen=True, slots=True)
class GraphExtractionService:
    """Read one chunk, or report honestly that it could not be read."""

    executor: AgentExecutor
    #: Bounds the whole call including the corrective turn. Ingestion waits on
    #: this per chunk, so it is a promise to the document behind it.
    timeout_seconds: float
    sink_for: Callable[[Identifier], EventSink]

    async def extract(
        self, principal: PrincipalContext, *, text: str
    ) -> ExtractionResult:
        if not text.strip():
            # Nothing to read. Reported as extracted, because an empty chunk
            # genuinely names nothing and no model call would change that.
            return ExtractionResult(extraction=_NOTHING, extracted=True)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await self._extract(principal, text=text)
        except TimeoutError:
            return ExtractionResult(extraction=_NOTHING, extracted=False)
        except Exception:  # a chunk must not take the document down with it
            return ExtractionResult(extraction=_NOTHING, extracted=False)

    async def _extract(
        self, principal: PrincipalContext, *, text: str
    ) -> ExtractionResult:
        stream_id = new_id(EXTRACTION_STREAM_PREFIX)
        sink = self.sink_for(stream_id)
        passage = user_message(f"Passage:\n{text}")

        outcome = await self.executor.run(
            self._request(principal, stream_id=stream_id, messages=(passage,)),
            sink,
            NullCancellationToken(),
        )
        if outcome.status != "completed":
            return ExtractionResult(extraction=_NOTHING, extracted=False)
        try:
            return ExtractionResult(
                extraction=_decode(outcome.output_text), extracted=True
            )
        except StructuredOutputFramingError:
            pass
        except StructuredOutputError:
            return ExtractionResult(extraction=_NOTHING, extracted=False)

        corrected = await self.executor.run(
            self._request(
                principal,
                stream_id=stream_id,
                messages=(passage, *restatement_messages(outcome.output_text)),
            ),
            sink,
            NullCancellationToken(),
        )
        if corrected.status != "completed":
            return ExtractionResult(extraction=_NOTHING, extracted=False)
        try:
            return ExtractionResult(
                extraction=_decode(corrected.output_text), extracted=True
            )
        except StructuredOutputError:
            return ExtractionResult(extraction=_NOTHING, extracted=False)

    def _request(
        self,
        principal: PrincipalContext,
        *,
        stream_id: str,
        messages: tuple[Message, ...],
    ) -> AgentRunRequest:
        return AgentRunRequest(
            trace=TraceContext(agent_run_id=new_agent_run_id()),
            run_kind="chat",
            stream_id=stream_id,
            principal=principal,
            # Deny-shaped: an extractor reads a passage and holds no tools.
            envelope=AuthorizationEnvelope(),
            system_prompt=_EXTRACTION_CONTRACT,
            messages=messages,
            budget=_EXTRACTION_BUDGET,
            model_profile="compact",
        )


def _decode(text: str) -> ChunkExtraction:
    json_object(text)
    try:
        document = _DOCUMENT.validate_json(text, strict=True)
    except ValidationError as error:
        raise StructuredOutputError("extraction output has an invalid shape") from error
    return ChunkExtraction(
        entities=tuple(
            ExtractedEntity(name=entity.name, entity_type=entity.entity_type)
            for entity in document.entities
        ),
        relations=tuple(
            ExtractedRelation(
                subject=relation.subject,
                object=relation.object,
                description=relation.description,
            )
            for relation in document.relations
        ),
    )


def graph_identity(
    *, extraction_model: str, prompt_version: str, embedder_identity: str
) -> str:
    """What a graph *is*, for the rows written under it.

    Three parts because three things change what was extracted: the model that
    read the passage, the prompt it read under, and the embedder whose vectors
    the arms will match against. Rows disagreeing on any of them are not
    comparable, and nominating from two identities at once would let a
    re-extraction change retrieval without changing anything a reader can see.
    """

    return f"{extraction_model}+{prompt_version}+{embedder_identity}"


__all__ = [
    "MAX_ENTITIES_PER_CHUNK",
    "MAX_RELATIONS_PER_CHUNK",
    "ExtractionResult",
    "GraphExtractionService",
    "graph_identity",
]
