"""A real PDF, assembled byte by byte.

Shared rather than private to one test module: two suites need a PDF with
known page boundaries, and importing a helper out of a ``test_`` module works
only when that module's directory happens to be on ``sys.path`` -- which is
true when pytest has collected it and false in a fresh process. CI found the
difference; this is the fix that does not depend on collection order.

Built here rather than produced by a writer library so the fixture stays
dependency-free, and so the page boundaries are something a test *states*
rather than discovers -- which is usually the property under test.
"""

from __future__ import annotations


def build_pdf(pages: tuple[str, ...]) -> bytes:
    """One Helvetica text run per page, in a valid cross-referenced file."""

    streams = []
    for text in pages:
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        streams.append(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode())

    count = len(pages)
    page_ids = [3 + index for index in range(count)]
    content_ids = [3 + count + index for index in range(count)]
    font_id = 3 + 2 * count

    objects: list[tuple[int, bytes]] = [(1, b"<< /Type /Catalog /Pages 2 0 R >>")]
    kids = " ".join(f"{identifier} 0 R" for identifier in page_ids).encode()
    objects.append((2, b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % count))
    for page_id, content_id in zip(page_ids, content_ids, strict=True):
        objects.append(
            (
                page_id,
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                % (font_id, content_id),
            )
        )
    for content_id, stream in zip(content_ids, streams, strict=True):
        objects.append(
            (
                content_id,
                b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            )
        )
    objects.append((font_id, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))

    # The binary marker real producers emit right after the header, so a
    # consumer treats the file as binary. It also makes this fixture what a
    # PDF actually is: not valid UTF-8, which one of the tests below depends
    # on and which a pure-ASCII fixture would quietly falsify.
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for number, body in objects:
        offsets[number] = len(out)
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    start_xref = len(out)
    size = max(offsets) + 1
    out += b"xref\n0 %d\n" % size + b"0000000000 65535 f \n"
    for number in range(1, size):
        out += b"%010d 00000 n \n" % offsets.get(number, 0)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        size,
        start_xref,
    )
    return bytes(out)
