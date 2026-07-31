"""Which citations an answer actually earned.

Returning the retrieval packet's citations says "these passages were found". It
does not say the answer used them, and the two were being reported as the same
thing -- so an answer that ignored every passage still shipped with a full set
of sources under it, and one that leaned on a single chunk shipped with seven.
A reader checking a claim against a citation it was never drawn from finds
nothing and concludes the system is wrong about its own sources.

So a citation is offered only when the model named it *and* the model was shown
it. Both halves matter, and the second is a boundary rather than a nicety: a
chunk id the run was never shown is a string the model produced, and echoing it
back would let a guessed identifier -- possibly a real one from somewhere this
asker cannot read -- be presented as a source with this system's authority.

What cannot be verified is dropped rather than downgraded. There is no
"probably" here: an answer's sources are the ones that can be pointed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agent_workbench.application.ingestion import CHUNK_ID_PREFIX
from agent_workbench.domain.context import Citation, ContextPacket

#: A chunk id wherever it appears, in whatever the model wrapped it in.
#:
#: This began as ``\[(...)\]``, matching the way the prompt labels evidence. The
#: first live run against a real provider showed why that was wrong: asked to
#: cite, DeepSeek wrote ``(chk_5793...)`` in parentheses, and an answer that
#: plainly named its source came back with no sources at all. The delimiter was
#: never the signal -- the *shape* is. A 32-hex-digit id under this prefix does
#: not occur in prose by accident, so it is recognised bare, bracketed,
#: parenthesised or in backticks.
#:
#: The shape comes from the one place that mints them rather than being copied,
#: so a chunker that changes its id format cannot silently stop being cited.
_CITED = re.compile(rf"\b({re.escape(CHUNK_ID_PREFIX)}_[0-9a-f]{{8,64}})\b")


@dataclass(frozen=True, slots=True)
class CitationVerdict:
    """What survived checking, and what was claimed but could not be shown."""

    #: Named by the answer and present in what the run was shown, in the order
    #: the evidence was retrieved -- not the order the model happened to mention
    #: them, which is the model's rhetoric rather than a fact about sources.
    verified: tuple[Citation, ...]
    #: Named by the answer and absent from everything it was shown. Reported
    #: rather than returned: it is a property of the answer worth counting, and
    #: never a source worth offering.
    fabricated: tuple[str, ...]

    @property
    def trustworthy(self) -> bool:
        """Whether every id the answer named could be pointed at."""

        return not self.fabricated


def cited_chunk_ids(answer: str) -> tuple[str, ...]:
    """The chunk ids an answer names, deduplicated, in first-mention order."""

    seen: dict[str, None] = {}
    for match in _CITED.finditer(answer):
        seen.setdefault(match.group(1), None)
    return tuple(seen)


def verify_citations(
    answer: str,
    shown: tuple[ContextPacket, ...],
) -> CitationVerdict:
    """Keep the citations this answer can point at, and count the ones it cannot.

    ``shown`` is every packet the run was given -- one for the fixed shape, one
    per search for the agentic one. Checking against the corpus instead would
    accept a citation to a real chunk this run never saw and this asker may not
    be allowed to read, which is the same disclosure by a longer route.
    """

    named = set(cited_chunk_ids(answer))
    if not named:
        return CitationVerdict(verified=(), fabricated=())

    available: dict[str, Citation] = {}
    for packet in shown:
        for citation in packet.citations:
            available.setdefault(citation.chunk_id, citation)

    verified = tuple(
        citation for chunk_id, citation in available.items() if chunk_id in named
    )
    fabricated = tuple(sorted(named - available.keys()))
    return CitationVerdict(verified=verified, fabricated=fabricated)


__all__ = ["CitationVerdict", "cited_chunk_ids", "verify_citations"]
