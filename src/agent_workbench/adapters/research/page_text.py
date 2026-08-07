"""Readable text out of a fetched HTML page.

Written on the standard library's ``HTMLParser`` rather than on a readability
package. The job here is narrow -- drop the markup and the parts of a page that
are never prose, keep the rest in reading order -- and a dependency that ships
its own heuristics would decide more than this needs decided.

What it is *not* is a content extractor that finds "the article". Pages this
runs on are as often a weather table as an essay, and a main-content heuristic
tuned for essays throws tables away. Everything readable is kept, and choosing
what matters is left to the step that reads it.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Final

#: Tags whose text is markup's business, never the reader's.
_SILENT_TAGS: Final[frozenset[str]] = frozenset(
    {"script", "style", "noscript", "template", "svg", "head", "iframe"}
)

#: Tags that end a line of prose. Without these, "丹东市" and "32" from two
#: table cells arrive as "丹东市32" and the reader cannot tell them apart.
_BREAKING_TAGS: Final[frozenset[str]] = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "textarea",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)

# Escaped rather than literal: a no-break space and an ideographic space are
# invisible in source, and Chinese pages are full of both.
_WHITESPACE = re.compile(r"[ \t\u00a0\u3000]+")
_BLANK_LINES = re.compile(r"\n{2,}")


class _Reader(HTMLParser):
    """Collects readable text, with a newline wherever a block ends."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._silenced = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in _SILENT_TAGS:
            self._silenced += 1
        elif tag in _BREAKING_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SILENT_TAGS:
            # Clamped at zero: a stray `</script>` with no opener is common in
            # real pages, and letting the counter go negative would silence the
            # entire rest of the document.
            self._silenced = max(0, self._silenced - 1)
        elif tag in _BREAKING_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._silenced == 0:
            self._parts.append(data)

    @property
    def text(self) -> str:
        return "".join(self._parts)


def page_text(html: str, *, limit: int) -> str:
    """The readable text of ``html``, collapsed and cut to ``limit`` chars.

    Malformed markup is not an error: ``HTMLParser`` is lenient by design, and
    a page too broken to parse yields whatever text was recovered before the
    break rather than an exception -- a partial read of a real page beats no
    evidence at all.
    """

    reader = _Reader()
    try:
        reader.feed(html)
        reader.close()
    except Exception:
        pass
    collapsed = _WHITESPACE.sub(" ", reader.text)
    lines = [line.strip() for line in collapsed.split("\n")]
    joined = "\n".join(line for line in lines if line != "")
    return _BLANK_LINES.sub("\n", joined).strip()[:limit]


__all__ = ["page_text"]
