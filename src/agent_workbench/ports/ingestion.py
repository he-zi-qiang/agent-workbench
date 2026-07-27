"""The ingestion boundaries: reading a document, and counting its tokens.

Both are ports because both will grow implementations that reach outside the
process. Reading is ``bytes.decode`` only while the formats are text; PDF
brings a library, encrypted files, scanned pages with no text layer, and
producers that disagree about where a page ends. Counting is arithmetic only
while it is an approximation; the model's own tokenizer is a download.

The token counter is a boundary for a second reason. Which tokenizer counts
decides where every chunk boundary falls, so it is part of what identifies an
index -- two counters are two different chunkings, and a name that did not say
which one would let a re-index move every offset a citation depends on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from agent_workbench.domain.context import SourceLocator


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """One document's text, and what it was read from."""

    text: str
    media_type: str


@dataclass(frozen=True, slots=True)
class TextChunk:
    """One window of a document, positioned in the original text."""

    ordinal: int
    text: str
    locator: SourceLocator


@runtime_checkable
class DocumentParser(Protocol):
    """Turns stored bytes into text this process can chunk."""

    def parse(self, content: bytes, *, media_type: str) -> ParsedDocument:
        """Decode a document, or refuse it.

        Refusing is the contract. Decoding unknown bytes with errors replaced
        produces a document full of replacement characters that chunks, embeds
        and retrieves like any other -- an answer grounded in text nobody
        wrote, with every layer downstream reporting success.
        """
        ...


@runtime_checkable
class TokenCounter(Protocol):
    """Counts tokens the way some particular model would."""

    @property
    def name(self) -> str:
        """Identifies this counting, for the index identity it feeds."""
        ...

    def count(self, text: str) -> int: ...

    def split(self, text: str) -> tuple[str, ...]:
        """The pieces ``count`` counts, in order, reassembling to ``text``.

        Returned rather than merely counted because a chunk has to be cut at a
        boundary this counter agrees with. Counting by one rule and cutting by
        another produces windows whose size was never actually measured.
        """
        ...


__all__ = [
    "DocumentParser",
    "ParsedDocument",
    "TextChunk",
    "TokenCounter",
]
