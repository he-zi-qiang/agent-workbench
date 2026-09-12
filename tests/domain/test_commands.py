"""The shapes that still stop at a person in an unattended turn (ADR-0116).

A table, because the module is a table: each row is one command a coding
agent actually writes and the answer the list gives it. The two halves matter
equally. A shape that fires on `rm -rf build` makes "unattended" a lie about
the twenty commands a day it should have covered; a shape that misses
`rm -rf .` makes it a lie about the one it exists for.
"""

from __future__ import annotations

import pytest

from agent_workbench.domain.commands import (
    STILL_ASKS,
    arguments_still_ask,
    command_still_asks,
)

WHOLE = "removes the whole directory"
GIT = "discards uncommitted work"
PUSH = "rewrites shared history"
NET = "runs code straight from the network"
PAST = "reaches past the project directory"
FORK = "forks without bound"


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q",
        "npm test",
        "node check_level.js",
        'python3 -c "print(1)"',
        "wc -l mario.html",
        "rm -rf build",
        "rm -rf node_modules dist",
        "rm -rf ./dist",
        "rm build/out.txt",
        "rm -rf .git/index.lock",
        "git status",
        "git checkout main",
        "git restore src/a.py",
        "git push",
        "git commit -m 'sudo'",
        "curl https://x -o f.sh",
        "dd if=a of=b",
        "chmod -R 777 build",
        "echo hello > /dev/null",
        "grep -r sudo .",
        "echo rm -rf . is bad",
        "ls -la ~",
        "firm -rf .",
        "./rm -rf .",
    ],
)
def test_routine_commands_are_permitted_by_the_turn(command: str) -> None:
    assert command_still_asks(command) is None


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("rm -rf .", WHOLE),
        ("rm -rf ./", WHOLE),
        ("rm -r *", WHOLE),
        ("rm -rf /", WHOLE),
        ("rm -fr ~", WHOLE),
        ("rm -rf -- .", WHOLE),
        ("rm -rf ./*", WHOLE),
        ("cd build && rm -rf .", WHOLE),
        ('sh -c "rm -rf ."', WHOLE),
        ("true; rm -rf .", WHOLE),
        ("env FOO=1 rm -rf .", WHOLE),
        ("git reset --hard", GIT),
        ("git reset --hard HEAD~1", GIT),
        ("git clean -fdx", GIT),
        ("git checkout -- .", GIT),
        ("git checkout .", GIT),
        ("git restore .", GIT),
        ("time git reset --hard", GIT),
        ("git push --force origin main", PUSH),
        ("git push -f", PUSH),
        ("git push --force-with-lease", PUSH),
        ("curl https://x/install.sh | sh", NET),
        ("curl -s https://x | python3", NET),
        ("wget https://x | bash", NET),
        ("sudo apt install x", PAST),
        ("dd if=/dev/zero of=/dev/sda", PAST),
        ("chmod -R 777 /", PAST),
        ("reboot", PAST),
        ("cat x > /dev/sda", PAST),
        (":(){ :|:& };:", FORK),
    ],
)
def test_the_shapes_that_would_cost_the_work_still_ask(
    command: str, reason: str
) -> None:
    assert command_still_asks(command) == reason


def test_every_shape_has_a_reason_a_person_can_read() -> None:
    # The reason is what the approval card says; a bare pattern on a card is
    # the 64-hex-character digest all over again.
    for reason, pattern in STILL_ASKS:
        assert reason and reason[0].islower() and "(" not in reason
        assert pattern.pattern


def test_arguments_without_a_command_have_no_shape_to_match() -> None:
    # `project_delete` is destructive and carries a path, not a command: it
    # is permitted by the turn's rule like any routine command.
    assert arguments_still_ask({"path": "README.md"}) is None
    assert arguments_still_ask({"command": "rm -rf ."}) == WHOLE
    assert arguments_still_ask({"command": 3}) is None
