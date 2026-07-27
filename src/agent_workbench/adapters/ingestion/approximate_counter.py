"""A stand-in for a model tokenizer, until the model tokenizer is here.

Words and punctuation. Not the same as any real tokenizer, and named so that
nothing mistakes it for one: sub-word models split longer and rarer words into
several pieces, so this undercounts, and a 512-token window measured here holds
more than a model would call 512 tokens.

It exists so ingestion is deterministic and testable without a multi-gigabyte
download in the loop. When the model's own tokenizer arrives it replaces this
one -- and because the name is part of the index identity, that replacement is
a re-index rather than a quiet drift in where every boundary falls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Words, numbers, and any other single non-space character.
_WORD = re.compile(r"\w+|[^\w\s]")


@dataclass(frozen=True, slots=True)
class ApproximateTokenCounter:
    """Words and punctuation, standing in for a model's tokenizer."""

    name: str = "approx-word-v1"

    def count(self, text: str) -> int:
        return len(_WORD.findall(text))

    def split(self, text: str) -> tuple[str, ...]:
        """Pieces that concatenate back to the original, whitespace included."""

        pieces: list[str] = []
        cursor = 0
        for match in _WORD.finditer(text):
            pieces.append(text[cursor : match.end()])
            cursor = match.end()
        if cursor < len(text):
            # Trailing whitespace joins the last piece rather than being
            # dropped: chunk offsets have to index the original text exactly.
            if pieces:
                pieces[-1] += text[cursor:]
            else:
                pieces.append(text[cursor:])
        return tuple(pieces)


__all__ = ["ApproximateTokenCounter"]
