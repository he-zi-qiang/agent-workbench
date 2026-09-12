"""The three holes in the browser's seccomp profile, and only those three.

ADR-0113 §3.5 replaced `--no-sandbox` with a profile that is Docker's default
with three specific edits. Docker offers no way to say "default, plus this" --
only whole-profile replacement -- so the artifact in `docker/` is a snapshot
that can be edited by anyone, at which point nothing would notice a fourth hole.
These tests are what notices. They run offline: the artifact is in the tree and
nothing here fetches or launches anything.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROFILE = Path(__file__).resolve().parents[2] / "docker" / "chromium-seccomp.json"

#: The mask upstream uses: all seven namespace bits must be zero.
UPSTREAM_MASK = 0x7E020000
#: Ours: the same, minus CLONE_NEWUSER, CLONE_NEWPID and CLONE_NEWNET.
EXPECTED_MASK = 0x0E020000


def _profile() -> dict[str, Any]:
    return json.loads(PROFILE.read_text(encoding="utf-8"))


def test_the_profile_still_denies_by_default() -> None:
    """The single most important property, and the cheapest one to lose."""

    assert _profile()["defaultAction"] == "SCMP_ACT_ERRNO"


def test_clone_opens_exactly_three_namespace_bits() -> None:
    """Not `unconfined`, not `SYS_ADMIN`: three bits, named.

    NEWNS, NEWCGROUP, NEWUTS and NEWIPC stay refused, exactly as upstream
    refuses them.
    """

    masks = [
        argument["value"]
        for group in _profile()["syscalls"]
        for argument in group.get("args") or []
        if argument.get("op") == "SCMP_CMP_MASKED_EQ"
    ]
    assert masks, "the clone namespace rules are gone entirely"
    assert set(masks) == {EXPECTED_MASK}, [hex(mask) for mask in masks]
    assert UPSTREAM_MASK not in masks


def test_unshare_is_opened_under_the_same_mask_as_clone() -> None:
    """The zygote reaches its user namespace through `unshare`, not `clone`.

    Carrying the same mask over keeps this hole the same shape as the first
    one; allowing `unshare` outright would be a wider one wearing its name.
    """

    rules = [
        group
        for group in _profile()["syscalls"]
        if group["names"] == ["unshare"] and group["action"] == "SCMP_ACT_ALLOW"
    ]
    assert len(rules) == 1, rules
    arguments = rules[0].get("args") or []
    assert [argument["value"] for argument in arguments] == [EXPECTED_MASK]


def test_chroot_is_allowed_and_nothing_else_was_added() -> None:
    """Three edits. A fourth would be a decision nobody wrote down."""

    profile = _profile()

    # Upstream already carries a `chroot` rule gated on CAP_SYS_CHROOT; ours is
    # the ungated one next to it, and the distinction is the test. Inside the
    # user namespace the zygote holds that capability, so the kernel would
    # permit the call -- seccomp refuses it anyway because a per-process filter
    # does not know about namespaces.
    chroot_rules = [
        group for group in profile["syscalls"] if group["names"] == ["chroot"]
    ]
    ungated = [group for group in chroot_rules if "includes" not in group]
    assert len(ungated) == 1, chroot_rules
    assert ungated[0]["action"] == "SCMP_ACT_ALLOW"

    # A fourth hole would look like this: a capability-guarded syscall moved
    # into an *ungated* allow. Upstream's own groups name `mount` and
    # `pivot_root` under `includes: {caps: [CAP_SYS_ADMIN]}`, and that gate is
    # what must survive -- the container has no capabilities, so a gated rule
    # never fires here.
    for group in profile["syscalls"]:
        if group["names"] in (["unshare"], ["chroot"]):
            continue
        if group["action"] != "SCMP_ACT_ALLOW" or group.get("includes"):
            continue
        for dangerous in ("mount", "pivot_root", "setns", "bpf"):
            assert dangerous not in group["names"], (dangerous, group["names"])


def test_the_profile_records_where_it_came_from() -> None:
    """A snapshot that does not say what it is a snapshot of cannot be upgraded."""

    generated = _profile().get("_generated_by", "")
    assert "make_chromium_seccomp.py" in generated
    assert "moby" in generated
    assert "ADR-0113" in generated


def test_the_session_actually_asks_playwright_for_the_sandbox() -> None:
    """The profile above buys nothing unless the launch opts in.

    This is the assertion the rest of this file assumed and did not make.
    `LAUNCH_FLAGS` carefully omits `--no-sandbox`, and a comment there explains
    why -- but Playwright's `chromium_sandbox` defaults to **False** and passes
    that flag itself. Measured 2026-09-12 in a container: under Docker's
    default seccomp profile, which refuses `CLONE_NEWUSER` and so cannot run a
    sandboxed Chromium at all, `launch()` started one and rendered a page.
    Only an unsandboxed browser can do that.

    So every part of ADR-0113 §3.5 -- the generated profile, its three holes,
    the A/B -- was describing a layer the code had turned off. Nothing failed:
    the pages rendered, the tools worked, the tests passed. A protection that
    is absent looks exactly like one that is present until something attacks
    it, which is why the opt-in is asserted here rather than reviewed.

    With it on, the same default-profile container refuses to launch at all
    ("Chromium sandboxing failed!"), and that is the failure mode worth having.
    """

    source = (
        PROFILE.parents[1] / "src/agent_workbench/apps/browser_mcp/session.py"
    ).read_text(encoding="utf-8")

    assert "chromium_sandbox=True" in source, (
        "Playwright defaults this to False and passes --no-sandbox; without "
        "the explicit opt-in the seccomp profile in this file is decoration"
    )
    # The other half: it must not come back by the front door either. Matched
    # with its quotes, because the prose above the flag tuple discusses the
    # flag by name and a bare substring check would fail on the explanation.
    assert '"--no-sandbox"' not in source
    assert "'--no-sandbox'" not in source
