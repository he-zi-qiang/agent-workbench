"""What an extractor may claim about one chunk, and what a claim is worth.

Two shapes, and the difference between them is the ADR-037 argument in types.
An ``ExtractedEntity`` is a *way in* -- it merges with the same name in the
same knowledge base, so two documents describing team Marlin become one entry
point. An ``EntityMention`` is *evidence*, and it never merges: it names the
exact chunk the claim was read from, which is what lets retrieval nominate
that chunk and lets authorization decide by its document.

Nothing here carries a score or a rank. An extractor says what a chunk
mentions; how much that is worth against a query is measured later, by the
same RRF every other arm goes through.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

from pydantic import Field

from agent_workbench.domain.schema import DomainModel

#: How long an entity name may be before it is refused. Generous, because a
#: name is a phrase in some corpora ("the aw-core cluster"), and bounded
#: because it is model output that becomes a database key.
MAX_ENTITY_NAME = 512
#: A relationship description is what the relation arm embeds, so it is a
#: sentence rather than a label -- and still bounded for the same reason.
MAX_RELATION_DESCRIPTION = 2048

_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")


def normalize_entity_name(name: str) -> str:
    """The merge key for a name, and the only place it is computed.

    Case, surrounding punctuation and internal whitespace are all things two
    documents differ on while meaning one thing, so they are normalised away.
    Unicode is folded to NFKC first, because "ﬁle" and "file" are the same
    word to a reader and different bytes to a unique constraint.

    Deliberately *not* stemming or stripping articles. Those are language
    decisions this project has not made, and a merge key that guesses at them
    would silently join two entities a corpus keeps apart -- which, unlike
    failing to merge, cannot be noticed from the outside.
    """

    folded = unicodedata.normalize("NFKC", name).strip().casefold()
    collapsed = _WHITESPACE.sub(" ", folded)
    return collapsed.strip("\"'`.,;:!?()[]{}<>")


class ExtractedEntity(DomainModel):
    """One thing a chunk names, as the extractor saw it."""

    name: str = Field(min_length=1, max_length=MAX_ENTITY_NAME)
    entity_type: str = Field(min_length=1, max_length=64)

    @property
    def normalized_name(self) -> str:
        return normalize_entity_name(self.name)


class ExtractedRelation(DomainModel):
    """One edge a chunk asserts, in the extractor's own words.

    ``description`` is what the relation arm embeds and therefore what a query
    is matched against, so it has to read as a sentence about the pair rather
    than as a label -- "team Marlin carries the Cinder rotation", not
    "carries".
    """

    subject: str = Field(min_length=1, max_length=MAX_ENTITY_NAME)
    object: str = Field(min_length=1, max_length=MAX_ENTITY_NAME)
    description: str = Field(min_length=1, max_length=MAX_RELATION_DESCRIPTION)


class ChunkExtraction(DomainModel):
    """Everything one chunk yielded, before any of it is stored.

    Both lists may be empty and that is an answer, not a failure: a chunk of
    boilerplate mentions nothing worth an entry point. What must not happen is
    an empty result standing in for an extraction that never ran -- the
    caller distinguishes those, because one is a fact about the chunk and the
    other is a fact about the model.
    """

    entities: tuple[ExtractedEntity, ...] = ()
    relations: tuple[ExtractedRelation, ...] = ()

    def relations_with_known_entities(self) -> tuple[ExtractedRelation, ...]:
        """Edges whose endpoints this chunk also named.

        An extractor routinely asserts an edge to something it did not list as
        an entity. Storing those would create entity rows nothing ever
        mentioned -- entry points with no evidence behind them, which is
        exactly the merged-graph failure ADR-037 refuses. Dropped rather than
        repaired, because inventing the missing entity would be this code
        making the claim.
        """

        known = {entity.normalized_name for entity in self.entities}
        return tuple(
            relation
            for relation in self.relations
            if normalize_entity_name(relation.subject) in known
            and normalize_entity_name(relation.object) in known
        )


__all__ = [
    "MAX_ENTITY_NAME",
    "MAX_RELATION_DESCRIPTION",
    "ChunkExtraction",
    "ExtractedEntity",
    "ExtractedRelation",
    "normalize_entity_name",
]
