"""Cutting a document into overlapping windows.

The window is measured in tokens, and which tokenizer counts them changes where
every boundary falls. That makes the counter part of what identifies an index,
not an implementation detail: two chunkings under one name would mean a
re-index silently moves boundaries, and citations built against the old offsets
would point at text that is no longer there.

So a counter carries a name, the chunker's identity is built from it, and the
identity travels into every chunk id. An index built with an approximate
counter is therefore visibly not the same index as one built with the model's
own tokenizer -- which is the point, because it isn't.

Overlap exists so that a sentence spanning a boundary is retrievable from
either side. Without it, the one chunk that answers a question can be the one
holding half the answer.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_workbench.domain.context import SourceLocator
from agent_workbench.ports.ingestion import TextChunk, TokenCounter


@dataclass(frozen=True, slots=True)
class Chunker:
    """Fixed-size overlapping windows over one document's text."""

    size_tokens: int
    overlap_tokens: int
    counter: TokenCounter

    def __post_init__(self) -> None:
        if self.size_tokens < 1:
            raise ValueError("size_tokens must be positive")
        if self.overlap_tokens < 0:
            raise ValueError("overlap_tokens must not be negative")
        if self.overlap_tokens >= self.size_tokens:
            # Each window would start no later than the previous one, so the
            # cursor never advances and the document never ends.
            raise ValueError("overlap_tokens must be smaller than size_tokens")

    @property
    def identity(self) -> str:
        """What this chunking is, for the index built from it."""

        return f"{self.counter.name}-{self.size_tokens}-{self.overlap_tokens}"

    def split(self, text: str) -> tuple[TextChunk, ...]:
        pieces = self.counter.split(text)
        if not pieces:
            return ()

        # Where each piece begins in the original, so a chunk can be located
        # exactly rather than found again by searching for its own text --
        # which would land on the wrong copy whenever a passage repeats.
        starts: list[int] = []
        cursor = 0
        for piece in pieces:
            starts.append(cursor)
            cursor += len(piece)

        step = self.size_tokens - self.overlap_tokens
        chunks: list[TextChunk] = []
        for ordinal, begin in enumerate(range(0, len(pieces), step)):
            window = pieces[begin : begin + self.size_tokens]
            if not window:
                break
            body = "".join(window)
            char_start = starts[begin]
            chunks.append(
                TextChunk(
                    ordinal=ordinal,
                    text=body,
                    locator=SourceLocator(
                        char_start=char_start,
                        char_end=char_start + len(body),
                    ),
                )
            )
            if begin + self.size_tokens >= len(pieces):
                # The window already reached the end. Continuing would emit
                # further windows that are all suffixes of this one.
                break
        return tuple(chunks)


__all__ = ["Chunker"]
