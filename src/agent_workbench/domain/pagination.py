"""A position in a list, for lists that are still being written to.

Offset pagination is wrong here and not merely slower. These lists are ordered
newest first and grow at the newest end, so a second page fetched by offset
re-reads rows the first page already returned and skips exactly as many as
arrived in between. The reader never learns which ones it lost.

A keyset cursor names the last row delivered, so "the next page" means "older
than this one" no matter what has been inserted since. It is ordered by
``(created_at, id)`` rather than ``created_at`` alone because two rows can share
a timestamp, and a cursor that could not separate them would either repeat one
or drop one every time it landed on a tie.

The cursor is opaque to clients but deliberately not encrypted or signed. It
carries a timestamp and an id the caller was just shown, so there is nothing in
it to protect -- and pretending otherwise would suggest a page it can be used to
reach is authorized by holding it. Authorization is re-checked per request from
the caller's own identity; this only says where to continue.

It is base64url so that it survives being a query parameter. The unencoded form
ends in a UTC offset, and ``+00:00`` in a query string decodes to a space -- so
the plain form works everywhere except the one place cursors are actually sent,
and fails there by silently becoming a cursor for a different time.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Final

from pydantic import ValidationError

from agent_workbench.domain.errors import IncompatibleSchemaError
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.schema import DomainModel

#: Not permitted in an identifier and not produced by ``isoformat``, so neither
#: half can contain it and the split is unambiguous.
CURSOR_SEPARATOR: Final[str] = "|"

#: A cursor arrives in a query string. Bounded so a decoder is never handed an
#: arbitrary amount of text to parse.
MAX_CURSOR_LENGTH: Final[int] = 192


class ListCursor(DomainModel):
    """The last row a page delivered, in ``(created_at, id)`` order."""

    created_at: datetime
    last_id: Identifier

    def encode(self) -> str:
        raw = f"{self.created_at.isoformat()}{CURSOR_SEPARATOR}{self.last_id}"
        # Unpadded: "=" is legal in a query string but is the one character
        # here that invites a client to strip it.
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, raw: str) -> ListCursor:
        """Parse a client-supplied cursor, failing closed on anything odd.

        Every rejection raises the same error with the same text. A decoder that
        explained which half it disliked would describe the id namespace to
        whoever was guessing at it.
        """

        if not raw or len(raw) > MAX_CURSOR_LENGTH:
            raise IncompatibleSchemaError("malformed list cursor")
        try:
            padded = raw + "=" * (-len(raw) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise IncompatibleSchemaError("malformed list cursor") from exc
        timestamp, separator, last_id = decoded.partition(CURSOR_SEPARATOR)
        if not separator:
            raise IncompatibleSchemaError("malformed list cursor")
        try:
            return cls(created_at=datetime.fromisoformat(timestamp), last_id=last_id)
        except (ValidationError, ValueError) as exc:
            raise IncompatibleSchemaError("malformed list cursor") from exc


__all__ = ["CURSOR_SEPARATOR", "MAX_CURSOR_LENGTH", "ListCursor"]
