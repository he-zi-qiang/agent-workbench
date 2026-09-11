"""The desktop icon that starts the stack, and the icon it wears.

`scripts/stack.cmd` is the Windows way in, and Explorer will run it on a
double-click -- but it looks like every other `.cmd` on the machine, because
Windows takes a file's icon from its *type* and there is no per-file override.
`scripts/shortcut.cmd` therefore generates a `.lnk`, which can carry an icon and
can be pinned; `scripts/make_icon.py` draws the icon it points at.

None of that can be executed here: this suite runs on POSIX, and the interesting
half of the generator is a PowerShell program driving a COM object. So each test
below asserts the *rule* that makes the Windows behaviour hold -- on bytes the
runner can read -- and says which rule it stands for, the same bargain
`test_architecture_panel.py` makes. A rule is weaker evidence than a run, and
`docs/windows-quickstart.md` says so where somebody follows it.
"""

from __future__ import annotations

import importlib.util
import re
import struct
import zlib
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts" / "shortcut.cmd"
LAUNCHER = ROOT / "scripts" / "stack.cmd"
ICON = ROOT / "scripts" / "agent-workbench.ico"
ICON_SOURCE = ROOT / "scripts" / "make_icon.py"

#: How cmd.exe assembles the PowerShell program: a chain of `set "PS=%PS% ..."`
#: lines, each one appending to what the previous ones built.
_FRAGMENT = re.compile(r'^set "PS=(?P<value>.*)"$')


def _source() -> str:
    return GENERATOR.read_bytes().decode("ascii")


def _commands() -> str:
    """Its executable lines only.

    Every "this must not appear" rule below is about what the file *does*, and
    its comments quote the failures they exist to prevent -- including the
    spellings those rules forbid. A naive search over the whole file matches the
    explanation and reports the opposite of the truth. `test_compose.py` learned
    this on the same shape of assertion.
    """

    return "\n".join(
        line
        for line in _source().splitlines()
        if not line.lstrip().lower().startswith("rem")
    )


def _assembled_program() -> str:
    """The PowerShell program as cmd.exe would hand it to powershell.exe."""

    program = ""
    for line in _source().splitlines():
        match = _FRAGMENT.match(line.strip())
        if match:
            program = match.group("value").replace("%PS%", program)
    assert program, 'no `set "PS=..."` fragments found; this guard is stale'
    return program


def test_the_generator_keeps_the_two_batch_conventions() -> None:
    """ASCII and CRLF, the same two rules `stack.cmd` and `panel.cmd` are held to.

    cmd.exe reads a batch file in the console's OEM code page rather than UTF-8,
    so a Chinese character in the file itself arrives as mojibake on most
    installs -- which is why the shortcut's Chinese *name* is spelled in code
    points inside the PowerShell program instead. CRLF because `goto` into a
    label is where LF-only batch files are known to misbehave.
    """

    raw = GENERATOR.read_bytes()
    raw.decode("ascii")
    assert b"\r\n" in raw
    assert re.search(rb"[^\r]\n", raw) is None, "every line ending must be CRLF"

    # No redirection, pipe or conditional character inside a rem line: cmd
    # splits a rem line on a conditional operator and runs what follows, so a
    # comment quoting a shell snippet is a command and its parse error prints on
    # every single run. Both other launchers carry this rule; this file's
    # comments are *about* quoting and ampersands, so it earns it twice over.
    talkative = [
        line
        for line in _source().splitlines()
        if line.lstrip().lower().startswith("rem") and any(c in line for c in "&|<>")
    ]
    assert not talkative, f"cmd.exe executes these rem lines: {talkative}"


def test_the_powershell_program_survives_being_parsed_twice() -> None:
    """The one rule that makes this file work, asserted rather than trusted.

    The program is parsed by cmd.exe before PowerShell ever sees it, and cmd
    counts quote characters and splits on conditional operators. A literal quote
    in the program flips cmd's idea of whether the rest of the line is quoted;
    an ampersand that then lands outside the quotes ends the command and runs
    whatever follows. The program needs both characters -- the shortcut's
    arguments are a quoted `set` chained to the launcher -- so it spells them
    [char]34 and [char]38 and contains neither.
    """

    program = _assembled_program()
    assert "[char]34" in program and "[char]38" in program
    assert '"' not in program, "a literal quote flips cmd's quoting for the rest"
    assert "&" not in program, "a literal ampersand can end the command line"

    # And the whole line, walked the way cmd.exe walks it: anything in `&|<>`
    # reached outside quotes would be where the command stops.
    invocations = [
        line.strip()
        for line in _source().splitlines()
        if line.strip().startswith("powershell ") and "%PS%" in line
    ]
    assert len(invocations) == 1, invocations
    line = invocations[0].replace("%PS%", program)
    quoted = False
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char in "&|<>" and not quoted:
            raise AssertionError(f"cmd.exe would stop the command at {index}: {line}")
    assert not quoted, "the invocation leaves a quote open"


def test_the_shortcut_wires_the_launcher_the_icon_and_the_mirror() -> None:
    """What the generated `.lnk` is actually made of.

    The Chinese name is asserted as code points because the file may not contain
    the characters themselves: U+5DE5 U+4F5C U+53F0.
    """

    commands = _commands()
    program = _assembled_program()

    assert "%~dp0stack.cmd" in commands, "the shortcut must point at the launcher"
    assert "%~dp0agent-workbench.ico" in commands
    assert "$lnk.IconLocation=" in program
    for code_point in ("0x5DE5", "0x4F5C", "0x53F0"):
        assert code_point in program, f"the shortcut's name lost {code_point}"

    # GetFolderPath rather than USERPROFILE\Desktop: with OneDrive's Known
    # Folder Move on -- the default on a great many Windows 11 machines -- the
    # real desktop is under OneDrive and USERPROFILE\Desktop is a leftover
    # folder nobody looks at. Getting this wrong puts the icon somewhere the
    # person cannot see, which is indistinguishable from the script failing.
    assert "[Environment]::GetFolderPath('Desktop')" in program
    assert "USERPROFILE" not in commands

    # The mirror is *read* and never defaulted: which endpoint a deployment's
    # weights come from is a supply-chain decision belonging to whoever runs the
    # stack, which is why `docker/fetch_weights.py` does not pick one either.
    # Carrying it here is the only way a double-click has ever been able to
    # have it -- an icon inherits no terminal.
    assert 'set "AW_LNK_MIRROR=%HF_ENDPOINT%"' in commands
    assert "hf-mirror" not in commands and "huggingface" not in commands

    # `remove` exists, so the icon is not a one-way door.
    assert 'if /i "%~1"=="remove"' in commands


def _icon_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_icon", ICON_SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _check_png(payload: bytes, side: int) -> None:
    """Walk one entry's chunks and check the checksums the format already has.

    This is what keeps the *other* eight sizes honest without redrawing them. A
    byte flipped anywhere inside a chunk -- a checkout that mangled the file, a
    half-written commit -- leaves the ICO directory and the IHDR dimensions
    perfectly valid, so an icon that merely parses proves very little. PNG
    carries a CRC per chunk; using it costs nothing.
    """

    assert payload.startswith(b"\x89PNG\r\n\x1a\n"), f"{side}px entry is not a PNG"
    offset = 8
    tags: list[bytes] = []
    while offset < len(payload):
        (length,) = struct.unpack_from(">I", payload, offset)
        tag = payload[offset + 4 : offset + 8]
        body = payload[offset + 8 : offset + 8 + length]
        (crc,) = struct.unpack_from(">I", payload, offset + 8 + length)
        assert crc == zlib.crc32(tag + body) & 0xFFFFFFFF, (
            f"{side}px entry: {tag!r} chunk fails its own checksum"
        )
        tags.append(tag)
        offset += 12 + length
    assert offset == len(payload), f"{side}px entry has trailing bytes"
    assert tags[0] == b"IHDR" and tags[-1] == b"IEND", tags


def test_the_icon_is_a_multi_size_ico_that_its_generator_still_draws() -> None:
    """The committed binary, and its tie to the source that produced it.

    A `.ico` in a repository is the kind of file that drifts away from whatever
    drew it and then cannot be changed by anybody. Regenerating all nine sizes
    costs about three seconds of pure-Python rasterising, which is too much to
    spend on every run, so this compares one size byte for byte -- enough to
    fail the moment the geometry, the colours or the encoder move without the
    committed icon moving with them.
    """

    raw = ICON.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", raw, 0)
    assert (reserved, kind) == (0, 1), "not an ICO directory"
    assert count >= 2

    sizes: dict[int, bytes] = {}
    for index in range(count):
        offset = 6 + 16 * index
        side, _, _, _, _, bpp, length, start = struct.unpack_from(
            "<BBBBHHII", raw, offset
        )
        # A 256-pixel image records its side as 0: the field is one byte and the
        # format has always spelled it that way.
        side = side or 256
        payload = raw[start : start + length]
        _check_png(payload, side)
        width, height = struct.unpack_from(">II", payload, 16)
        assert (width, height) == (side, side), f"{side}px entry declares {width}"
        assert bpp == 32, "an icon without an alpha channel gets a box around it"
        sizes[side] = payload

    # 16 is what Explorer draws in a list view -- the size every more
    # interesting mark this repository tried turned to mush at -- and 256 is
    # what the preview pane asks for.
    assert 16 in sizes and 256 in sizes

    assert sizes[32] == _icon_module().png(32), (
        "scripts/agent-workbench.ico no longer matches scripts/make_icon.py; "
        "run `python scripts/make_icon.py`"
    )
