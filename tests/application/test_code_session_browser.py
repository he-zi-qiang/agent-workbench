"""A coding session can be handed a browser, and is told so (ADR-0113 §4).

ADR-0113 decided the browser goes to "Code 会话与 Task 图节点" and shipped one
of those two. The Task half is `[[mcp.servers]]`: a Worker discovers the six
tools at startup and every Task envelope frozen afterwards names them. The Code
half had nothing -- no switch, no slot, no append -- so a coding session asked
to check the page it had just written answered, correctly, that it had no
browser, in a console whose 浏览器 panel was sitting next to it showing `503`.

What these tests hold is the seam that was missing, and they are deliberately
about the *offer* rather than about driving a browser: whether a real Chromium
answers is `tests/apps/test_browser_mcp_server.py`'s question and needs the
`browser` extra, which CI asserts is absent. What can be checked offline is the
part that was wrong -- that the names reach a turn at all, on both halves of
the session, and that the prompt agrees with the tools.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from agent_workbench.adapters.memory import (
    InMemoryArtifactStore,
    InMemoryConversationStore,
)
from agent_workbench.adapters.tools import StaticToolRegistry
from agent_workbench.application.code_session import (
    CODE_PROJECT_TOOLS,
    CODE_TOOLS,
    CodeSessionService,
    _system_prompt_for,
)
from agent_workbench.application.workspace_scope import WorkspaceScope
from agent_workbench.domain.runs import RunBudget
from agent_workbench.domain.tools import ToolName

NOW = datetime(2026, 9, 12, tzinfo=UTC)

#: What `adapters/mcp/naming.py` builds from the alias and the six remote names.
#: Spelled out rather than derived, so a change to the scheme fails here with
#: the old and new names side by side instead of agreeing with itself.
BROWSER_NAMES: tuple[ToolName, ...] = cast(
    tuple[ToolName, ...],
    (
        "mcp_browser_browser_open",
        "mcp_browser_browser_snapshot",
        "mcp_browser_browser_eval",
        "mcp_browser_browser_interact",
        "mcp_browser_browser_screenshot",
        "mcp_browser_browser_diagnostics",
    ),
)


def _service(**extra: object) -> CodeSessionService:
    return CodeSessionService(
        conversations=InMemoryConversationStore(),
        artifacts=InMemoryArtifactStore(),
        executor_for=lambda _scope: cast(Any, None),
        scope=WorkspaceScope(),
        budget=RunBudget(max_steps=2, max_tool_calls=2),
        turn_timeout_seconds=60,
        max_concurrent_turns=1,
        clock=lambda: NOW,
        tools=StaticToolRegistry([]),
        **cast(Any, extra),
    )


def test_a_session_with_no_browser_is_offered_none() -> None:
    """The default, and the reason the field is a callable returning ``()``.

    A deployment that configured no browser and one whose browser answered with
    nothing are the same thing to a turn, and the default has to be the second
    of those rather than ``None`` -- otherwise every read below needs a branch
    and one of them will eventually forget it.
    """

    service = _service()

    assert service._offered(project=False) == CODE_TOOLS
    assert service._offered(project=True) == CODE_PROJECT_TOOLS


def test_the_browser_names_reach_both_halves_of_a_session() -> None:
    """The orthogonality claim, which is why this is not a fifth tuple.

    `sandbox_run` belongs to the flat side and `project_run` to the project
    side because of what those tools *are* -- one reads a ContextVar the other
    side never sets, the other needs a directory. A browser tool needs neither:
    it drives a process in another container and hands back text. Written as
    tuples it would take the four literals to sixteen, which is the shape
    ADR-077 deleted and `CODE_PROJECT_TOOLS_WITH_RUN`'s comment argues against
    at length.

    Asserted on both halves in one test on purpose: the claim is that they
    agree, and two tests that each check one half would both pass on a change
    that made them disagree.
    """

    service = _service(browser_tools=lambda: BROWSER_NAMES)

    flat = service._offered(project=False)
    project = service._offered(project=True)

    assert flat == (*CODE_TOOLS, *BROWSER_NAMES)
    assert project == (*CODE_PROJECT_TOOLS, *BROWSER_NAMES)


def test_the_names_are_read_per_turn_and_not_copied_at_assembly() -> None:
    """The whole reason this field is a callable (ADR-025's freeze, from here).

    `apps/api/dependencies.py` builds the service synchronously and fills
    `BrowserSlot` in `startup`, which is async and runs afterwards. A tuple
    copied at assembly would therefore be the empty one on exactly the
    deployments that have a browser -- a failure that looks like the
    configuration not working and is actually two lifecycles crossing.

    Simulated here with a list the test mutates, because that is precisely what
    the slot does to itself between assembly and the first request.
    """

    discovered: list[ToolName] = []
    service = _service(browser_tools=lambda: tuple(discovered))

    assert service._offered(project=False) == CODE_TOOLS

    discovered.extend(BROWSER_NAMES)

    assert service._offered(project=False) == (*CODE_TOOLS, *BROWSER_NAMES)


def test_a_turn_holding_a_browser_is_told_what_it_is_for() -> None:
    """The prompt arm, and the sentence that decides how it gets used.

    ADR-058's comment makes the general argument from the other direction: a
    model behaves correctly for the world it was described as being in. Told
    nothing, a turn holding six unexplained `mcp_browser_*` tools treats them
    as a way to read documentation -- which is what `web_search` is for, costs
    a page render per lookup, and meets the destination guard on most of them.
    """

    without = _system_prompt_for(CODE_TOOLS, external_requires_approval=False)
    with_it = _system_prompt_for(
        CODE_TOOLS, external_requires_approval=False, browser=True
    )

    assert "browser_diagnostics" not in without
    assert "browser_diagnostics" in with_it
    # The two claims worth pinning, because they are the two a turn gets wrong:
    # that this is for checking its own work, and that the guard's refusals are
    # the guard rather than a bug in the page it just wrote.
    assert "checking your own work" in with_it
    assert "guard" in with_it


def test_the_browser_paragraph_does_not_unsay_the_network_claim() -> None:
    """Why `with_browser` appends and `with_web_search` rewrites.

    A turn holding `web_search` can put an arbitrary question on the open web,
    so the base prompt's "you cannot reach the network" has to be unsaid before
    that tool is offered. A turn holding the browser cannot: ADR-0113's
    Chromium reaches the network only through a proxy it cannot address, every
    request judged by `address_guard`, and nothing the turn writes changes
    where it may go. The sentence stays true, so it stays.

    This is the assertion that would fail if somebody "fixed" the asymmetry by
    making the two arms alike.
    """

    base = _system_prompt_for(CODE_TOOLS, external_requires_approval=False)
    with_it = _system_prompt_for(
        CODE_TOOLS, external_requires_approval=False, browser=True
    )

    # Whatever the base says about the network, the browser arm leaves intact:
    # it is a suffix and nothing else.
    assert with_it.startswith(base)
