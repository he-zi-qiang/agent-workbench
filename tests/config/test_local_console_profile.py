"""The combined profile the console runs (config.demo-local.toml).

The two narrow profiles each demonstrate one capability and are pinned apart by
`test_local_web_mcp_profile.py`. This one is the union, and it exists because a
console is one application: a Task submitted from Work carries whatever the API
froze into its envelope, and on the web profile a request for a Word document
had no renderer in that envelope at all. What the model did instead is pinned in
`tests/adapters/test_workspace_tools.py` -- it wrote Markdown into a file called
`report.docx`.

So the assertions here are about the union being *complete*: both servers, both
budget corrections, and the export gate this repository now declines by default
(ADR-038 §2.1 made it a choice; ADR-048 answered it). A profile missing any one
of them fails in a way that looks like the model misbehaving.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agent_workbench.application.sub_agents import CODE_SUB_AGENTS
from agent_workbench.bootstrap.projections import (
    project_api,
    project_task,
    project_task_worker,
)
from agent_workbench.bootstrap.settings import (
    Settings,
    WorkflowSettings,
    load_settings,
)
from agent_workbench.domain.agents import WORKSPACE_TOOL_NAMES

ROOT = Path(__file__).resolve().parents[2]
DEMO_CONFIG = ROOT / "config/config.demo-local.toml"
LOCAL_CONFIG = ROOT / "config/config.local.toml"
DEFAULT_CONFIG = ROOT / "config/config.default.toml"
POSTGRES_DSN = (
    "postgresql+asyncpg://agent:local-profile-test@127.0.0.1:5433/agent_workbench_local"
)


def _load_profile(monkeypatch: pytest.MonkeyPatch, path: Path) -> Settings:
    for name in tuple(os.environ):
        if name.upper().startswith("AW_"):
            monkeypatch.delenv(name, raising=False)
    for suffix in ("DSN", "GUARD_DSN", "LISTEN_DSN"):
        monkeypatch.setenv(f"AW_DATABASE__{suffix}", POSTGRES_DSN)
    return load_settings(config_file=path)


def test_the_console_profile_carries_both_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _load_profile(monkeypatch, DEMO_CONFIG)

    assert settings.optional_labs.mcp_adapter is True
    assert sorted(server.alias for server in settings.mcp.servers) == ["web", "word"]


def test_a_task_from_this_profile_can_reach_the_word_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The envelope is the assertion, not the server list.

    A Task carries the tool names the API froze at submission, and that freeze
    is the whole mechanism: `task_9bb8446a...` was submitted by a principal
    holding `mcp:word`, on a deployment whose config declared no Word server,
    and the scope bought nothing because the name was never in the envelope.
    """

    envelope = project_task(
        _load_profile(monkeypatch, DEMO_CONFIG)
    ).default_authorization_envelope

    assert "mcp_word_render_document" in envelope.allowed_tools
    assert "mcp_web_fetch_page" in envelope.allowed_tools
    assert "mcp_web_download_document" in envelope.allowed_tools


def test_the_writer_gets_word_and_the_researcher_gets_the_web(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audience, not just presence (ADR-027 §3.3).

    Both under one audience would be wrong in either direction: a writer able to
    read the outside world, or a renderer the writing node cannot see.
    """

    worker = project_task_worker(_load_profile(monkeypatch, DEMO_CONFIG))

    assert worker.mcp is not None
    audiences = {server.alias: server.audience for server in worker.mcp.servers}
    assert audiences == {"word": "synthesis", "web": "research"}


def test_the_console_profile_names_its_own_chat_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read from the file, with every AW_ variable stripped first.

    `_load_profile` deletes them, which is the assertion: `routed` has to come
    from the profile and not from whatever the shell or a launcher exported.
    It used to come from a wrapper, and the console's Chat therefore searched
    the web when started one way and not the other -- with no way to tell which
    you had short of asking it a question the corpus does not cover.

    The threshold is asserted beside the shape because it is only read under it:
    a shape that reverted to `fixed` would leave this line configuring nothing,
    silently, exactly as it did before.
    """

    settings = _load_profile(monkeypatch, DEMO_CONFIG)
    shipped = _load_profile(monkeypatch, DEFAULT_CONFIG)

    assert settings.chat.retrieval_shape == "routed"
    assert settings.chat.routed_relevance_threshold == pytest.approx(0.01)
    # Still a profile's choice, not a new default for every deployment.
    assert shipped.chat.retrieval_shape == "fixed"


def test_a_deployment_that_says_nothing_does_not_gate_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default ADR-048 chose, and the profiles that still say it themselves.

    ADR-038 §2.1 made this a deployment's choice and §4 required a new ADR to
    move the repository default; ADR-048 is that ADR. This assertion is what
    makes its decision checkable -- a later change that quietly restored the
    gate would have to edit this line and say why.

    The console profile is asserted too, and it is not redundant: a profile that
    states its own posture keeps stating it after the default agrees, so that it
    does not silently depend on a default that has now moved once.
    """

    demo = _load_profile(monkeypatch, DEMO_CONFIG)
    shipped = _load_profile(monkeypatch, DEFAULT_CONFIG)

    assert shipped.workflow.export_requires_approval is False
    assert demo.workflow.export_requires_approval is False
    # And the field's own default, which the shipped config never consults
    # because it states the value explicitly. Left unasserted, flipping the
    # Python default back is a change no test notices -- and the reader who
    # opens `settings.py` to find out what ships would be told the opposite of
    # what does. Constructed rather than loaded: the point is the default.
    assert WorkflowSettings.model_fields["export_requires_approval"].default is False


def test_the_console_profile_raises_both_budgets_a_document_run_needs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measured ceilings, carried over from the two narrow profiles.

    Both were hit for real: `budget_exceeded: max_steps` on a render-then-revise
    loop, and `budget_exceeded: token_budget` before the node rendered anything.
    Neither failure names a budget in the console, so they read as the model
    giving up halfway.
    """

    demo = _load_profile(monkeypatch, DEMO_CONFIG)
    shipped = _load_profile(monkeypatch, DEFAULT_CONFIG)

    # The measured ceilings moved into the shipped default (ADR-059's cleanup:
    # a default every working profile had to raise was a trap, not a default),
    # so the console inherits `max_steps` rather than silently diverging again.
    assert shipped.runtime.max_steps == 40
    assert shipped.multi_agent.max_tokens_per_agent_invocation == 120_000
    assert demo.runtime.max_steps == shipped.runtime.max_steps

    # The token ceiling is the one place the console *does* diverge, and it is
    # not a drift -- it is arithmetic this profile is forced into by turning
    # delegation on.
    #
    # `max_tokens_per_agent_invocation` is one graph node's ceiling, and
    # `application/delegation.py` hands a child `parent // children_allowed`.
    # With delegation on and `max_children_per_run = 6`, the shipped 120_000
    # leaves each sub-agent 20_000 -- measured against 36 real sub-agent runs on
    # this machine, where the largest spent 19_870 and one ended on
    # `stop_reason = token_budget`. So the console multiplies by the fan-out to
    # put each child back at the measured-adequate 120_000.
    #
    # Pinned as an equation rather than as the literal 720_000: if anybody
    # changes `max_children_per_run` again, this fails and says why, instead of
    # letting every sub-agent quietly get poorer the way the 4 -> 6 change did.
    assert demo.multi_agent.delegation_enabled is True
    assert (
        demo.multi_agent.max_tokens_per_agent_invocation
        == shipped.multi_agent.max_tokens_per_agent_invocation
        * demo.multi_agent.max_children_per_run
    )


def test_the_ordinary_local_profile_is_untouched_by_this_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The union is a new file, not an edit to a narrow one.

    Anybody running the default local setup must be unaffected: the tool names
    are frozen into every new Task's envelope, and that envelope is re-applied
    on resume.
    """

    settings = _load_profile(monkeypatch, LOCAL_CONFIG)

    assert settings.optional_labs.mcp_adapter is False
    assert settings.mcp.servers == ()


def _dev(
    command: str, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one arm of the script with no provider key from anywhere.

    `AW_KEY_FILE=""` is not decoration. The script reads a key file outside the
    checkout when the variable is unset, so on the one machine that actually has
    a key -- the developer's -- every refusal asserted below would quietly stop
    being a refusal. A test that only holds on a machine without the credential
    is not testing the arm that ships.
    """

    return subprocess.run(
        ["bash", "scripts/dev.sh", command],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHON": "/bin/echo",
            "AW_KEY_FILE": "",
            **(environment or {}),
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


def test_dev_script_probes_both_servers_before_either_is_assumed() -> None:
    result = _dev("demo-check")

    assert result.returncode == 0
    probed = [line.split() for line in result.stdout.splitlines() if line.strip()]
    assert [line[2] for line in probed] == ["word", "web"]
    assert "render_document" in probed[0]
    assert "fetch_page" in probed[1]
    assert "download_document" in probed[1]


def test_demo_worker_refuses_to_fake_the_task_path_without_a_provider() -> None:
    result = _dev("demo-worker", {"AW_SECRETS__DEEPSEEK_API_KEY": ""})

    assert result.returncode == 2
    assert "requires AW_SECRETS__DEEPSEEK_API_KEY" in result.stderr
    assert result.stdout.strip() == ""


def test_demo_worker_probes_both_servers_then_starts_the_real_graph(
    tmp_path: Path,
) -> None:
    """Which config file the arm exported, and in what order it checked.

    MCP discovery happens once at Worker startup and never hot-reloads, so a
    probe that ran after the Worker -- or only against one of the two servers --
    would let it come up missing the tool the profile exists for.
    """

    probe = tmp_path / "python-probe"
    probe.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$AW_CONFIG_FILE\"\nprintf '%s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    probe.chmod(0o700)
    result = subprocess.run(
        ["bash", "scripts/dev.sh", "demo-worker"],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHON": str(probe),
            "AW_SECRETS__DEEPSEEK_API_KEY": "contract-only-not-a-real-key",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "config/config.demo-local.toml",
        "-m agent_workbench.apps.task_worker.main",
    ]
    assert "--label word --endpoint http://127.0.0.1:8765/mcp" in result.stderr
    assert "--label web --endpoint http://127.0.0.1:8767/mcp" in result.stderr


def test_demo_api_refuses_a_console_whose_front_half_would_be_missing() -> None:
    """The dangerous half of the keyless start, and why it is a refusal.

    `demo-worker` already exited 2 without a key; `demo-api` printed two lines
    to stderr and started anyway. What it started was not a smaller console:
    `_assemble_chat` catches `ModelNotConfiguredError`, so neither `chat.router`
    nor `events.router` is mounted, and this profile's `triage.enabled = true`
    is left with no model, which drops every Task submitted from Work back to
    the v1 graph.

    None of that is visible from a browser -- `/ui` serves, all six pages
    render, Chat draws the same empty state it draws on a working start -- so
    the first evidence is a question that cannot be answered. A start that
    removes the feature the profile exists for has to fail where it can still
    be read.
    """

    result = _dev("demo-api", {"AW_SECRETS__DEEPSEEK_API_KEY": ""})

    assert result.returncode == 2
    assert "requires AW_SECRETS__DEEPSEEK_API_KEY" in result.stderr
    # Refused before the process was replaced: `exec` under `PYTHON=/bin/echo`
    # would have printed the module line.
    assert result.stdout.strip() == ""
    # And it names the deliberate way to get a chat-less API, which stays legal.
    assert "--without-chat" in result.stderr


def test_the_ordinary_api_arm_still_starts_without_a_key() -> None:
    """The control group for the refusal above.

    Only the console profile refuses. `dev.sh api` keyless is a deployment that
    indexes and searches and says it has no chat -- true of what it serves, and
    nothing about it claims otherwise. Without this assertion the refusal could
    spread to the ordinary arm and no test would notice.
    """

    result = _dev("api", {"AW_SECRETS__DEEPSEEK_API_KEY": ""})

    assert result.returncode == 0
    assert "-m agent_workbench.apps.api.main" in result.stdout
    assert "search without chat" in result.stderr


def test_demo_api_uses_the_same_profile(tmp_path: Path) -> None:
    probe = tmp_path / "python-probe"
    probe.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$AW_CONFIG_FILE\"\nprintf '%s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    probe.chmod(0o700)
    result = subprocess.run(
        ["bash", "scripts/dev.sh", "demo-api"],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHON": str(probe),
            "AW_KEY_FILE": "",
            "AW_SECRETS__DEEPSEEK_API_KEY": "contract-only-not-a-real-key",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines()[0] == "config/config.demo-local.toml"


def test_the_key_file_outside_the_checkout_is_what_every_arm_reads(
    tmp_path: Path,
) -> None:
    """One key source for the script, and it lives outside the working tree.

    Before this, the only thing that read the key from disk was a launcher
    wrapper outside `scripts/`. So the start documented in `docs/running-locally
    .md` had no provider while the wrapper's did, and the difference showed up
    as "Chat searched the web when I rehearsed and did not when I recorded".

    Outside the checkout for a second reason: `zip -r` and Finder's "Compress"
    honour no ignore file, and the CI secret scan reads commit history, where
    this credential has never been. A path under `$HOME` is not reachable by
    either mistake.
    """

    key_file = tmp_path / "key"
    key_file.write_text("  from-outside-the-checkout\n", encoding="utf-8")
    probe = tmp_path / "python-probe"
    probe.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$AW_SECRETS__DEEPSEEK_API_KEY\"\n",
        encoding="utf-8",
    )
    probe.chmod(0o700)
    environment = {
        **os.environ,
        "PYTHON": str(probe),
        "AW_KEY_FILE": str(key_file),
    }
    environment.pop("AW_SECRETS__DEEPSEEK_API_KEY", None)

    result = subprocess.run(
        ["bash", "scripts/dev.sh", "demo-api"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0
    # Whitespace-stripped: a key file written by a shell redirect ends in a
    # newline, and a provider header carrying one is rejected as a bad token.
    assert result.stdout.splitlines()[0] == "from-outside-the-checkout"


def test_an_exported_key_still_beats_the_file(tmp_path: Path) -> None:
    """The file is a fallback, not an override.

    A shell that already exported a key is the one place the developer stated an
    intent, and a file quietly winning over it would make "which key did that
    run use" unanswerable from the command line -- which is the same class of
    surprise as a wrapper being the only thing that loaded one.
    """

    key_file = tmp_path / "key"
    key_file.write_text("from-the-file\n", encoding="utf-8")
    probe = tmp_path / "python-probe"
    probe.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$AW_SECRETS__DEEPSEEK_API_KEY\"\n",
        encoding="utf-8",
    )
    probe.chmod(0o700)

    result = subprocess.run(
        ["bash", "scripts/dev.sh", "demo-api"],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHON": str(probe),
            "AW_KEY_FILE": str(key_file),
            "AW_SECRETS__DEEPSEEK_API_KEY": "from-the-shell",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines()[0] == "from-the-shell"


def test_the_usage_banner_lists_every_demo_command() -> None:
    """The banner is a `sed` range over the script's own header.

    Adding a command without extending the range leaves it undocumented, and
    the range is exactly the kind of thing that is never noticed by hand.
    """

    script = (ROOT / "scripts/dev.sh").read_text(encoding="utf-8")
    documented = {
        line.split()[2]
        for line in script.splitlines()
        if line.startswith("#   scripts/dev.sh ")
    }
    banner = _dev("")

    for command in ("demo-check", "demo-api", "demo-worker"):
        assert command in documented
        assert command in banner.stdout


CODE_CONFIG = ROOT / "config/config.code-local.toml"


@pytest.mark.parametrize("path", [DEMO_CONFIG, CODE_CONFIG], ids=["demo", "code"])
def test_a_profile_that_thinks_hard_gives_the_call_time_to_finish(
    monkeypatch: pytest.MonkeyPatch, path: Path
) -> None:
    """A profile on `reasoning_effort = high` must not keep the shipped 120s.

    Measured 2026-08-27 on this machine, against the real provider: of 13 model
    calls in one console run, **5 died at exactly 120.0s** and the longest
    survivor was 107.0s. Before `high` landed the same event log tops out at
    45-77s, so what moved the distribution is the effort setting -- and raising
    it without raising these two is the trade the profile comments themselves
    warn against, one layer further in than the layer they fixed.

    The pairing is the half worth pinning. `runtime/agent_runtime.py:122`: the
    runtime's envelope wraps the adapter's own per-request timeout and **the
    shorter of the two fires first**, so a profile that raises one and not the
    other has moved nothing. Both shipped at 120, which made that a tie.
    """

    for name in tuple(os.environ):
        if name.upper().startswith("AW_"):
            monkeypatch.delenv(name, raising=False)
    for suffix in ("DSN", "GUARD_DSN", "LISTEN_DSN"):
        monkeypatch.setenv(f"AW_DATABASE__{suffix}", POSTGRES_DSN)
    # `config.code-local.toml` turns `research.enabled` on, which refuses to
    # assemble against a placeholder key. Nothing here makes a call; the value
    # exists only so the profile loads. Set after the AW_* sweep above, which
    # is why this does not go through `_load_profile`.
    monkeypatch.setenv("AW_SECRETS__DEEPSEEK_API_KEY", "contract-only-not-a-real-key")
    settings = load_settings(config_file=path)
    main = settings.model.main

    assert main.reasoning_effort == "high"
    assert main.timeout_seconds > 120
    assert settings.runtime.model_timeout_seconds > 120
    # The third number `high` invalidated, and the one no prompt can work
    # around. `max_output_tokens` bounds **thinking plus answer**, so a profile
    # that turns reasoning up has to raise it or nodes die mid-thought.
    # Measured 2026-08-28: an `understand` turn ended at `output_tokens =
    # 16382` against the shipped 16384 while emitting 2,127 characters of
    # text -- roughly 14,900 tokens of reasoning for a step whose product is a
    # restatement. Tightening that node's prompt in the same batch cut its text
    # from 17,857 characters to 2,127 and the run still died, which is what
    # makes this a number rather than a wording problem.
    assert main.max_output_tokens > 16_384
    # The specific one fires first; the runtime's is the backstop. Equal values
    # make which one reports the failure a coin toss, and they answer to
    # different operators -- one is "this provider was slow", the other is
    # "this runtime gave up".
    assert settings.runtime.model_timeout_seconds > main.timeout_seconds
    # And both stay inside the turn, so one stuck call cannot eat the whole of
    # it -- the turn timeout has to remain the outer stop.
    assert main.timeout_seconds < settings.code.turn_timeout_seconds


def test_the_shipped_default_still_documents_the_pair_it_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`config.default.toml` keeps 120 on both, and that is deliberate.

    It ships `not-configured-deepseek-main`, a placeholder whose call durations
    this repository cannot measure -- the same reason it refuses to ship
    `pricing` or `context_window_tokens`. What it must not do is leave the
    coupling undocumented, because an operator who raises one of the two and
    sees no change has been told nothing about why.
    """

    shipped = _load_profile(monkeypatch, DEFAULT_CONFIG)
    assert shipped.model.main.timeout_seconds == 120
    assert shipped.runtime.model_timeout_seconds == 120

    text = DEFAULT_CONFIG.read_text(encoding="utf-8")
    assert "model_timeout_seconds" in text
    # Each site has to name the other, or the pair is only discoverable by
    # reading the runtime source.
    assert text.count("model_timeout_seconds") >= 2
    assert "timeout_seconds`" in text


def test_the_console_can_actually_delegate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The console profile has to answer the question its docs answer for it.

    `delegation_enabled` ships `false` and ADR-082 states why: with it off the
    tool is not registered and enters no Task's envelope, so "does this
    deployment have spawn" is a configuration question rather than a version
    question. This profile is the one the console actually runs, and until
    2026-08-28 it never answered that question -- while README described
    multi-agent as a capability.

    What that cost is on record, in a Task's own words:

        多 Agent 是模拟的：我是一个 agent，无法真正并行拉起多个独立 Agent；
        本次是将调研拆成 A/B/C/D 四个角色的分工并由汇总环节成稿。

    The model was not wrong. It was reporting its tool catalogue accurately,
    and a deployment that advertises spawn while shipping it off is one that
    makes the model cover for the configuration.
    """

    settings = _load_profile(monkeypatch, DEMO_CONFIG)

    assert settings.multi_agent.delegation_enabled is True
    # And the shipped default stays off, because this repository does not
    # decide for somebody else's deployment.
    assert (
        _load_profile(monkeypatch, DEFAULT_CONFIG).multi_agent.delegation_enabled
        is False
    )


def test_the_widest_tree_this_profile_permits_fits_its_task_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asserted here as well as in the validator, because turning delegation on
    is what makes that validator's arithmetic reachable at all -- and the two
    numbers it multiplies are both editable in this file."""

    multi = _load_profile(monkeypatch, DEMO_CONFIG).multi_agent
    widest = multi.max_children_per_run**multi.max_delegation_depth

    assert widest <= multi.max_agent_invocation_attempts_per_task


def test_there_is_one_console_launch_path_not_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No launch configuration may hand the console a capability the profile
    withholds.

    A second entry that exported `AW_MULTI_AGENT__DELEGATION_ENABLED=true`
    existed while the profile said nothing, and it was deleted when the profile
    spoke. Keeping both would make "does this console do multi-agent" a
    question about which command you typed rather than about the deployment.
    """

    launch = ROOT / ".claude/launch.json"
    if not launch.exists():  # pragma: no cover - the file is developer-local
        pytest.skip(".claude/launch.json is not present in this checkout")

    text = launch.read_text(encoding="utf-8")

    assert "AW_MULTI_AGENT__DELEGATION_ENABLED" not in text


@pytest.mark.parametrize("path", [DEMO_CONFIG, CODE_CONFIG], ids=["demo", "code"])
def test_a_code_session_may_delegate(
    monkeypatch: pytest.MonkeyPatch, path: Path
) -> None:
    """Code is the broadest agent this machine has, and until ADR-089 it was
    the only one that could not hand work off.

    Asserted on the projection rather than on the API's assembly because that
    is where the omission actually was: `ApiRuntimeConfig` had no
    `multi_agent` at all, so the process running Code turns could not have
    answered the question even if somebody had asked it.
    """

    for name in tuple(os.environ):
        if name.upper().startswith("AW_"):
            monkeypatch.delenv(name, raising=False)
    for suffix in ("DSN", "GUARD_DSN", "LISTEN_DSN"):
        monkeypatch.setenv(f"AW_DATABASE__{suffix}", POSTGRES_DSN)
    monkeypatch.setenv("AW_SECRETS__DEEPSEEK_API_KEY", "contract-only-not-a-real-key")

    api = project_api(load_settings(config_file=path))

    assert api.multi_agent is not None
    assert api.multi_agent.delegation_enabled is True
    # The two numbers that bound a Code delegation, there being no Task
    # registry here to charge and none permitted.
    assert api.multi_agent.max_children_per_run >= 1
    assert api.multi_agent.max_delegation_depth >= 1


@pytest.mark.parametrize("path", [DEMO_CONFIG, CODE_CONFIG], ids=["demo", "code"])
def test_a_fan_out_has_room_to_retry_one_failure(
    monkeypatch: pytest.MonkeyPatch, path: Path
) -> None:
    """`max_children_per_run` counts children *started*, failures included.

    That is deliberate (`application/delegation.py`): a run that could retry a
    failing child without cost could retry it forever, and every attempt spends
    real money. The consequence is that an N-way fan-out under an allowance of
    N has **no** tolerance for a single transient failure.

    Measured 2026-08-28 on this deployment: a research turn dispatched four
    analysts in one round, three returned, the fourth died on a provider
    `RemoteProtocolError`, and the retry was refused -- so that section of the
    report was written by the parent instead. The turn said so itself.

    Six rather than four therefore buys retries, not a wider fan-out. Asserted
    as a *relationship* rather than as the number six, because what matters is
    the headroom: a profile that later widens its fan-out has to widen this
    too, and an equality would pass while the tolerance quietly went to zero.
    """

    for name in tuple(os.environ):
        if name.upper().startswith("AW_"):
            monkeypatch.delenv(name, raising=False)
    for suffix in ("DSN", "GUARD_DSN", "LISTEN_DSN"):
        monkeypatch.setenv(f"AW_DATABASE__{suffix}", POSTGRES_DSN)
    monkeypatch.setenv("AW_SECRETS__DEEPSEEK_API_KEY", "contract-only-not-a-real-key")

    multi = load_settings(config_file=path).multi_agent

    # The observed fan-out width, plus room for at least two retries.
    assert multi.max_children_per_run >= 6
    # And the ceiling the validator enforces still holds, so raising one of
    # these without the other fails at startup rather than at run time.
    widest = multi.max_children_per_run**multi.max_delegation_depth
    assert widest <= multi.max_agent_invocation_attempts_per_task


def test_the_shipped_default_keeps_the_narrower_allowance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four, unchanged. This repository does not decide somebody else's cost
    ceiling -- the same rule that keeps `pricing`, `context_window_tokens` and
    `delegation_enabled` at their shipped values."""

    shipped = _load_profile(monkeypatch, DEFAULT_CONFIG)

    assert shipped.multi_agent.max_children_per_run == 4


def test_a_code_sub_agent_never_holds_a_working_set_tool() -> None:
    """`WORKSPACE_TOOL_NAMES` is refused at definition time, and ADR-089 does
    not soften it: a delegated run shares its parent's session rather than
    opening one of its own, so the version pinning that makes a replay produce
    another version instead of a second effect does not hold for it.

    The project-side read tools are a different matter and are how `explorer`
    is useful at all -- but it holds only the *read* three (ADR-0078: a read is
    a receipt, and a receipt belongs to the run that did the reading)."""

    for definition in CODE_SUB_AGENTS.definitions:
        assert not set(definition.tool_names) & WORKSPACE_TOOL_NAMES
        assert not any(
            name in {"project_write", "project_edit", "project_run"}
            for name in definition.tool_names
        )
