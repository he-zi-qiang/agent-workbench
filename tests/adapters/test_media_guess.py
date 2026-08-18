"""One table, and the divergence it replaced.

The bug this pins is not hypothetical and it is not about labels. Two suffix
tables existed -- one in ``workspace.py`` for what a model wrote, one in
``sandbox.py`` for what a script wrote -- and they disagreed about which
pictures exist. The console decides what a reader may *see* from the stored
media type, so the disagreement decided whether a produced chart was a picture
or a download, based on nothing but which half of the system produced it.
"""

from __future__ import annotations

import pytest

from agent_workbench.adapters.tools.media_guess import media_type_for


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # The four that were in neither table. A plotting script picks its
        # format from the filename it is handed, so `savefig("chart.png")` was
        # visible in the console and `savefig("chart.jpg")` -- the same picture
        # -- was download-only.
        ("chart.jpg", "image/jpeg"),
        ("chart.jpeg", "image/jpeg"),
        ("animation.gif", "image/gif"),
        ("shot.webp", "image/webp"),
        # In the sandbox's table and not the workspace tool's.
        ("plot.png", "image/png"),
        ("paper.pdf", "application/pdf"),
        # In both, and unchanged.
        ("notes.md", "text/markdown"),
        ("rows.csv", "text/csv"),
        ("sq.py", "text/x-python"),
        ("page.html", "text/html"),
        ("data.json", "application/json"),
        # The row with a recorded incident behind it (ADR-062 §2): typed as
        # text by one writer and as a byte stream by the other, a drawn diagram
        # reached no viewer at all.
        ("diagram.svg", "image/svg+xml"),
        # Config and source, readable rather than precisely typed: the only
        # question this console asks about them is "can it be read".
        ("config.yaml", "text/plain"),
        ("pyproject.toml", "text/plain"),
        ("run.sh", "text/plain"),
        ("app.tsx", "text/plain"),
    ],
)
def test_a_name_decides_the_type_whoever_wrote_the_file(
    name: str, expected: str
) -> None:
    # Both callers now ask one function, so the answer cannot depend on the
    # caller. The bytes are the *fallback*, so they change nothing here.
    assert media_type_for(name, b"") == expected
    assert media_type_for(name, b"\x00\x01\x02") == expected


def test_an_unknown_name_is_decided_by_its_bytes() -> None:
    # The merge's whole difficulty: the two old fallbacks were each right for
    # their own caller. `workspace_write`'s content is a JSON string and so is
    # always text; a sandbox output is base64-decoded and may be anything.
    # Asking the bytes gives both their old answer without either knowing who
    # it is.
    assert media_type_for("notes.unknown", "hello 世界".encode()) == "text/plain"
    assert (
        media_type_for("blob.unknown", b"\x89PNG\r\n\x1a\n\x00\x00")
        == "application/octet-stream"
    )


def test_an_undecodable_payload_is_not_called_text() -> None:
    # Invalid UTF-8 without a NUL: the second check has to be the decode, not
    # only the NUL scan.
    assert (
        media_type_for("x.unknown", b"\xff\xfe\xfd\xfc") == "application/octet-stream"
    )


def test_a_character_straddling_the_sniff_boundary_is_not_binary() -> None:
    # 8 KiB of ASCII followed by a multi-byte character cut in half by the
    # window. A naive decode of the head raises, and calling that binary would
    # make the answer depend on where a Chinese character happened to land.
    payload = b"a" * (8 * 1024 - 1) + "世".encode()
    assert media_type_for("long.unknown", payload) == "text/plain"


def test_empty_content_under_an_unknown_name_stays_readable() -> None:
    # No evidence against it, and the readable answer is the one a reader can
    # act on.
    assert media_type_for("empty.unknown", b"") == "text/plain"


def test_a_nul_byte_settles_it_even_in_otherwise_valid_utf8() -> None:
    assert media_type_for("x.unknown", b"text\x00more") == "application/octet-stream"
