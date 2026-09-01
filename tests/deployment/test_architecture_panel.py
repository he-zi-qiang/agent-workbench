"""The architecture panel has to open on a machine that has only Python.

That is the whole claim this file guards, and it is a claim about *portability*
rather than about output: the panel is the answer to "what is this repository",
so it must not require the answer to be known first. Concretely it must run on
Windows, where `scripts/dev.sh` cannot, and on a checkout where `uv sync` has
never been run.

None of this can be executed here -- the suite runs on POSIX -- so each test
asserts the *rule* that makes the Windows behaviour hold, on a tree the runner
can read. A rule asserted on the source is weaker evidence than a run, and the
tests say which rule they stand for so that a reader can tell the difference.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "scripts" / "architecture_panel.py"
LAUNCHER = ROOT / "scripts" / "panel.cmd"


def _panel_source() -> str:
    return PANEL.read_text(encoding="utf-8")


def test_the_panel_imports_nothing_outside_the_standard_library() -> None:
    """This is what "no `uv sync` first" rests on, so it is asserted, not assumed.

    Every other entry point in this repository needs the environment built
    before it will start. The panel is the one a person opens *because* they do
    not yet know what the repository is, and a first step that fails on a fresh
    checkout would put that backwards. One third-party import silently removes
    the property.
    """
    tree = ast.parse(_panel_source())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])

    outside = sorted(imported - set(sys.stdlib_module_names) - {"__future__"})
    assert not outside, (
        f"the panel imports {outside}, which means it now needs an installed "
        f"environment before it can tell anyone what this repository is"
    )

    # "In the standard library" is not the same question as "importable on
    # Windows". `sys.stdlib_module_names` is a property of the CPython source
    # tree and lists the POSIX-only extension modules on every platform, so
    # `import fcntl` would satisfy the assertion above and fail on the one
    # platform this file exists for.
    posix_only = {
        "fcntl",
        "grp",
        "pwd",
        "termios",
        "tty",
        "pty",
        "resource",
        "syslog",
        "posix",
        "spwd",
        "crypt",
        "nis",
        "readline",
        "curses",
    }
    unix_isms = sorted(imported & posix_only)
    assert not unix_isms, f"{unix_isms} does not import on Windows"


def test_every_path_the_panel_emits_is_posix_shaped() -> None:
    """`str(p.relative_to(root))` yields backslashes on Windows; `_rel` does not.

    The strings this produces are not only displayed. `scan_tools` decides which
    catalogue a tool belongs to by matching `adapters/tools/` against one of
    them, so on Windows the obvious spelling would not merely look wrong -- it
    would file every tool under the wrong heading, and the page would still
    render.
    """
    # Over the AST rather than the text: `_rel`'s own docstring quotes the wrong
    # spelling in order to explain why it is wrong, and a grep cannot tell the
    # difference between a call and a sentence about one. (It could not, and
    # said so, which is how this ended up parsed instead.)
    #
    # And through a temporary, because that is how the version of this test that
    # only matched `str(x.relative_to(y))` missed the real one: `scan_packages`
    # bound the result to `rel` on one line and wrote `str(rel)` nine lines
    # later, so every row of the 320-module browser -- the section advertised as
    # searchable by path -- carried backslashes on Windows, and a query
    # containing a slash matched nothing.
    tree = ast.parse(_panel_source())
    via_name = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "relative_to"
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    def _is_a_path(node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return node.id in via_name
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "relative_to"
        )

    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "str"
        and len(node.args) == 1
        and _is_a_path(node.args[0])
    ]
    assert not offenders, (
        f"str(...relative_to(...)) at line(s) {offenders} produces "
        f"backslash-separated paths on Windows; use _rel()"
    )

    # `_rel` is the only spelling allowed to produce one, and it must reach for
    # as_posix() rather than str(). (An assertion that PureWindowsPath.as_posix
    # returns forward slashes used to sit here. It tested pathlib, not this
    # program, and could not fail.)
    rel_fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_rel"
    )
    assert any(
        isinstance(node, ast.Attribute) and node.attr == "as_posix"
        for node in ast.walk(rel_fn)
    ), "_rel has to normalise through as_posix()"


def test_the_panel_makes_its_own_output_survive_a_non_utf8_console() -> None:
    """A redirected stdout on Windows is cp1252, and this program is Chinese.

    Narrower than "Windows", and the distinction is the point: a console stdout
    there has been UTF-8 since Python 3.6, but a redirected one falls back to
    the ANSI code page with strict errors. So `panel --json > data.json` raised
    UnicodeEncodeError *after* building everything it was asked for -- the
    failure shape where the work succeeded and the report is what broke.
    Reproduced on this POSIX runner with PYTHONIOENCODING=cp1252.
    """
    source = _panel_source()
    assert "def _speak_utf8()" in source
    assert 'reconfigure(encoding="utf-8", errors="replace")' in source

    # "somewhere inside main()" was the earlier assertion, and it was satisfied
    # by a call placed one line *after* parse_args -- which is where argparse
    # has already written every Chinese `help=` string in this file to an
    # unreconfigured stdout. `--help` is the invocation a new reader tries
    # first, so order is the whole property; membership is not.
    tree = ast.parse(source)
    main = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    calls = {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr: node.lineno
        for node in ast.walk(main)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Name, ast.Attribute))
    }
    assert "_speak_utf8" in calls, "main() has to reconfigure its own streams"
    assert calls["_speak_utf8"] < calls["ArgumentParser"], (
        "reconfigure before the parser exists: argparse prints from inside parse_args"
    )


def test_the_bind_that_is_meant_to_fail_says_why() -> None:
    """Windows is where this one actually fires, and where it is least readable.

    ``_LoopbackServer`` refuses ``SO_REUSEADDR`` there on purpose, so a second
    launch loses the bind rather than silently splitting requests with the
    first. That decision only pays if the refusal is a sentence: an unhandled
    OSError is a traceback, and an Explorer double-click closes the window on
    top of it.
    """
    tree = ast.parse(_panel_source())
    serve = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "serve"
    )
    guarded = [
        handler
        for node in ast.walk(serve)
        if isinstance(node, ast.Try)
        for handler in node.handlers
        if any(
            isinstance(call.func, ast.Name) and call.func.id == "_LoopbackServer"
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
        )
        and isinstance(handler.type, ast.Name)
        and handler.type.id == "OSError"
    ]
    assert guarded, "constructing the server has to catch OSError and explain it"


def test_the_page_is_written_with_one_newline_convention() -> None:
    """Otherwise the same tree builds two different files on two machines.

    Asserted on the keyword, not on the text. The first version of this checked
    that ``newline="\\n"`` appeared in the source -- which the *comment*
    explaining the keyword satisfies on its own, so deleting the keyword and
    keeping the comment would have kept this green.
    """
    tree = ast.parse(_panel_source())
    writes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "write_text"
    ]
    assert writes, "the panel no longer writes the page; this guard is stale"
    for call in writes:
        kwargs = {kw.arg for kw in call.keywords}
        assert {"encoding", "newline"} <= kwargs, (
            f"write_text at line {call.lineno} must pin both encoding and newline"
        )


def test_windows_gets_a_launcher_that_starts_the_same_program() -> None:
    """`dev.sh` is bash. On Windows the way in is this file, or nothing."""
    assert LAUNCHER.is_file(), "scripts/panel.cmd is the Windows entry point"
    raw = LAUNCHER.read_bytes()

    # ASCII, because cmd.exe reads a batch file in the console's OEM code page
    # rather than UTF-8: a Chinese comment in here arrives as mojibake.
    raw.decode("ascii")

    # CRLF, because `goto` into a label is where LF-only batch files are known
    # to misbehave, and this file is built out of gotos.
    assert b"\r\n" in raw
    assert re.search(rb"[^\r]\n", raw) is None, "every line ending must be CRLF"

    text = raw.decode("ascii")

    # No redirection, pipe or conditional character inside a rem line. Microsoft
    # documents that a batch comment may not contain them, and the documented
    # multi-line-comment idiom shows why: cmd splits a rem line on a conditional
    # operator and runs what follows. A comment here quoting a shell snippet is
    # a command, and the parse error it raises prints on every single run before
    # anything works. This file had one, in the comment explaining a different
    # cmd.exe parsing trap.
    talkative = [
        line
        for line in text.splitlines()
        if line.lstrip().lower().startswith("rem") and any(ch in line for ch in "&|<>")
    ]
    assert not talkative, f"cmd.exe executes these rem lines: {talkative}"

    assert "scripts\\architecture_panel.py" in text
    assert "--serve" in text

    # The %errorlevel%-inside-a-block trap, checked rather than named: inside a
    # parenthesised block the variable expands when the block is *parsed*, so it
    # holds whatever the value was before the block ran. The earlier version of
    # this assertion said `"goto :use_uv" in text`, which proves that a label
    # jump exists and nothing at all about the trap it was named after.
    def _block_delta(line: str) -> int:
        """Parens that open or close a command block, and only those.

        Two kinds do not: one inside a quoted string, and the `(` of `echo(` --
        the defensive echo idiom, which is one token rather than a block opener.
        Counting those made this walk believe the file was permanently one level
        deep from the double-click probe onwards, and flag three innocent lines.
        """
        body = re.sub(r'"[^"]*"', "", line)
        body = re.sub(r"(?i)\becho\(", "echo ", body)
        return body.count("(") - body.count(")")

    depth, trapped = 0, []
    for line in text.splitlines():
        code = "" if line.lstrip().lower().startswith("rem") else line
        if depth > 0 and "%errorlevel%" in code.lower():
            trapped.append(line.strip())
        depth = max(depth + _block_delta(code), 0)
    assert not trapped, (
        f"%errorlevel% inside a parenthesised block expands at parse time: {trapped}"
    )


def test_the_windows_paths_on_the_page_keep_their_backslashes() -> None:
    """A lone backslash inside a JavaScript template literal is an escape.

    The page is built by JS template literals, and ``scripts\\panel.cmd``
    written with one backslash reaches the reader as ``scriptspanel.cmd`` -- a
    command that does not exist, presented as the answer to "how do I open this
    on Windows". It rendered that way once. Nothing failed: the page built, the
    layout was fine, and the only broken thing was the instruction.
    """
    page = _panel_source().split('PAGE = r"""', 1)[1]
    for shown in ("panel.cmd", "architecture_panel.py --serve"):
        # Counted, because the loop's conditions include a CSS class name: if
        # the markup were restyled the body would stop running and this would
        # pass having checked nothing.
        checked = 0
        for line in page.splitlines():
            if shown in line and "scripts" in line and "mono" in line:
                checked += 1
                assert "scripts\\\\" in line, (
                    f"a Windows path in the page template needs its backslash "
                    f"doubled, or JS eats it: {line.strip()}"
                )
        assert checked, f"no page line shows {shown!r} any more -- guard is stale"


def test_neither_launcher_makes_the_panel_wait_for_an_environment() -> None:
    """The two ways this was nearly undone, one per platform.

    On POSIX ``dev.sh`` resolves every other command through ``$PYTHON``, which
    is ``.venv/bin/python`` -- the venv being exactly what you do not have on
    the checkout where this command is most useful. On Windows the obvious
    ``uv run python`` *syncs the project first*, so the fastest way to a page
    that needs no dependencies would have been to install all of them.

    Both were written that way first. Neither failed on a machine that already
    had the environment, which is every machine the author tested on.
    """
    dev = (ROOT / "scripts" / "dev.sh").read_text(encoding="utf-8")
    case = dev.split("\npanel)", 1)[1].split("\n  ;;", 1)[0]
    assert "python3" in case, (
        "dev.sh panel must fall back to a system python3; $PYTHON is the venv"
    )

    cmd = LAUNCHER.read_bytes().decode("ascii")
    assert "uv run --no-project" in cmd, (
        "plain `uv run` syncs the project before running -- the panel needs none of it"
    )
    order = [
        cmd.index("py -3 -c"),
        cmd.index("python -c"),
        cmd.index("uv --version"),
    ]
    assert order == sorted(order), (
        "uv must be probed last: it is the only branch that can be slow"
    )
    # Probed by running, not by `where`: Windows leaves a python.exe on PATH
    # even with no Python installed -- the Store's app-execution alias, which
    # opens a shop and exits 9009. `where` cannot tell it from an interpreter.
    #
    # Over the command lines only. The comment above that decision quotes
    # `where python` in order to explain it, and a whole-file search cannot tell
    # a command from a sentence about one -- the same trap this file already hit
    # once, one language over.
    commands = [
        line.strip()
        for line in cmd.splitlines()
        if line.strip() and not line.strip().lower().startswith("rem")
    ]
    assert not [line for line in commands if line.startswith("where ")], (
        "`where python` matches the Microsoft Store stub; run the candidate instead"
    )


def test_line_endings_are_pinned_rather_than_left_to_a_clone_s_settings() -> None:
    """The batch file's CRLF has to survive somebody else's `core.autocrlf`.

    Both directions are real failures, and the second is the common one: a
    Windows clone with ``autocrlf=true`` hands ``scripts/dev.sh`` to bash with
    CRLF, and bash answers ``/usr/bin/env: 'bash\r': No such file or
    directory`` -- an error naming neither the file nor the cause.
    """
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    rules = {
        line.split()[0]: line
        for line in attributes.splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert "eol=crlf" in rules.get("*.cmd", ""), "the Windows launcher needs CRLF"
    assert "eol=lf" in rules.get("*.sh", ""), "bash scripts must not arrive with CRLF"

    # And the file on disk agrees with the rule, which is the half a checkout
    # on any platform actually reproduces.
    assert b"\r\n" in LAUNCHER.read_bytes()


def _case_collisions(paths: Iterable[str]) -> list[str]:
    """Paths that differ only in case, which no Windows checkout can hold at once."""
    seen: dict[str, str] = {}
    clashes: list[str] = []
    for path in paths:
        first = seen.setdefault(path.lower(), path)
        if first != path:
            clashes.append(f"{first} vs {path}")
    return clashes


def test_no_two_tracked_files_differ_only_in_case() -> None:
    """A precondition for all of the above: the repository has to *clone* there.

    NTFS is case-insensitive, so two tracked paths differing only in case cannot
    both exist in a working tree. Git checks one out, silently leaves the other
    missing, and the clone is broken before anything is run. macOS hides this
    -- its default filesystem is case-insensitive too -- so the mistake is made
    on Linux and discovered on Windows.

    The control case is synthetic because this filesystem cannot produce a real
    one: the helper is handed a pair that does collide, and must say so.
    """
    assert _case_collisions(["docs/README.md", "docs/Readme.md"]) == [
        "docs/README.md vs docs/Readme.md"
    ]
    assert _case_collisions(["a/b.py", "a/c.py"]) == []

    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    ).stdout.split()
    assert tracked, "git ls-files returned nothing -- the check would pass vacuously"
    assert not _case_collisions(tracked)


def test_dev_sh_still_advertises_the_panel_in_its_own_usage() -> None:
    """`usage()` prints a fixed line range of that header, so it drifts silently."""
    script = (ROOT / "scripts" / "dev.sh").read_text(encoding="utf-8")
    header = [
        line for line in script.splitlines() if line.startswith("#   scripts/dev.sh ")
    ]
    assert any(line.startswith("#   scripts/dev.sh panel") for line in header)

    printed = re.search(r"usage\(\) \{ sed -n '2,(\d+)p'", script)
    assert printed is not None, "usage() no longer prints a line range"
    last_listed = max(
        i
        for i, line in enumerate(script.splitlines(), start=1)
        if line.startswith("#   scripts/dev.sh ")
    )
    assert int(printed.group(1)) >= last_listed, (
        "usage() stops printing before the last command it documents"
    )
