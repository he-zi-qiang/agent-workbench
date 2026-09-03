"""Static validation of the checked-in local Compose topology."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "compose.yaml"
WINDOWS_LAUNCHER = ROOT / "scripts" / "stack.cmd"


def test_compose_configuration_is_renderable_when_docker_is_available() -> None:
    """This parses the real Compose file without building or starting services."""

    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker is not installed")
    result = subprocess.run(
        [docker, "compose", "-f", str(COMPOSE_FILE), "config", "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def _compose() -> dict[str, object]:
    """The rendered topology, parsed. Skips when Docker is absent.

    Rendered rather than read: the file uses anchors and merge keys, so reading
    the YAML directly would assert what was written instead of what Compose
    actually produces.
    """

    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker is not installed")
    result = subprocess.run(
        [
            docker,
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "--profile",
            "demo",
            "config",
            # JSON rather than the default YAML: PyYAML is only present here
            # transitively, and a test that depends on a package nothing
            # declares breaks the day a resolver drops it.
            "--format",
            "json",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, dict)
    return parsed


def test_the_demo_topology_runs_two_task_workers() -> None:
    """Claim, lease, epoch and fencing only mean anything under contention.

    With one Worker every one of those invariants holds trivially, so a
    topology that ships one cannot demonstrate the part of this system that
    took the most work. The concurrency tests prove it against a real
    database; this is the version somebody can watch.
    """

    services = _compose()["services"]
    assert isinstance(services, dict)

    workers = {
        name: service
        for name, service in services.items()
        if name.startswith("task-worker")
    }
    assert len(workers) == 2, "the demo topology is a multi-worker one"


def test_neither_worker_pins_a_worker_id() -> None:
    """Each process mints its own at startup.

    An id set here would be an id two replicas could share, which is exactly
    the collision the lease is there to make impossible.
    """

    services = _compose()["services"]
    assert isinstance(services, dict)

    for name, service in services.items():
        if not name.startswith("task-worker"):
            continue
        environment = service.get("environment") or {}
        assert "AW_WORKER_ID" not in environment
        assert not any(key.endswith("WORKER_ID") for key in environment)


def test_the_api_serves_the_console_from_the_image() -> None:
    """A stack somebody can open, rather than routes somebody has to know.

    The console is copied into the image and served same-origin; the API
    refuses to start when the directory is missing, so a broken image fails at
    startup rather than in a browser.
    """

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "AS web-build" in dockerfile
    assert "pnpm install --frozen-lockfile" in dockerfile
    assert "pnpm build" in dockerfile
    assert "COPY --from=web-build --chown=app:app /build/web/dist ./web" in dockerfile
    assert "--web-dir /app/web" in (ROOT / "docker/run-api-local.sh").read_text(
        encoding="utf-8"
    )


def test_the_stack_has_the_collector_its_processes_export_to() -> None:
    """Not in the demo profile, and nothing depends on it.

    The API exports whether or not anybody opted into synthetic workers, so a
    collector behind ``--profile demo`` would leave the ordinary stack
    unobserved. And a `depends_on` would make a collector's problem into a
    run's problem, which is the one thing the telemetry factory refuses to do.
    """

    services = _compose()["services"]
    assert isinstance(services, dict)

    assert "otel-collector" in services
    collector = services["otel-collector"]
    assert "profiles" not in collector or not collector["profiles"]
    for name, service in services.items():
        if name == "otel-collector":
            continue
        assert "otel-collector" not in (service.get("depends_on") or {})


def test_the_exporter_port_is_the_one_the_collector_receives_on() -> None:
    """The defect this collector was added with, kept from coming back.

    The default endpoint was ``:4317`` while the exporter is OTLP over *HTTP*,
    which posts to ``<endpoint>/v1/traces``. 4317 is the gRPC port, so every
    span went to a listener that could not answer -- and telemetry fails open,
    so the stack looked healthy while recording nothing.

    Both numbers are read from the files that carry them. Asserting 4318 twice
    here would pass just as well with the two sides pointing at different
    ports.
    """

    configured = tomllib.loads(
        (ROOT / "config/config.default.toml").read_text(encoding="utf-8")
    )["observability"]["otel_exporter_endpoint"]
    exporter_port = urlsplit(configured).port

    collector = (ROOT / "docker/otel-collector.yaml").read_text(encoding="utf-8")
    receiver_ports = {
        int(match) for match in re.findall(r"endpoint:\s*0\.0\.0\.0:(\d+)", collector)
    }

    assert receiver_ports == {exporter_port}
    # The exporter appends the OTLP/HTTP paths, so an endpoint that already
    # carried one would produce `/v1/traces/v1/traces`.
    assert not urlsplit(configured).path


def test_the_api_and_host_proxy_use_distinct_ports() -> None:
    """The proxy must own the published port and health checks must cross it."""

    compose = _compose()
    api = compose["services"]["api"]

    assert api["environment"]["AW_API__PORT"] == "8001"
    assert api["environment"]["LOCAL_PROXY_PORT"] == "8000"
    assert api["environment"]["LOCAL_PROXY_UPSTREAM_PORT"] == "8001"
    assert "127.0.0.1:8000/health/ready" in " ".join(api["healthcheck"]["test"])

    proxy = (ROOT / "docker/loopback_proxy.py").read_text(encoding="utf-8")
    assert 'LOCAL_PROXY_UPSTREAM_PORT", "8001"' in proxy


def test_the_console_can_store_a_provider_key_outside_the_image() -> None:
    """The settings page needs one writable path in an otherwise read-only API.

    No key value belongs in Compose.  What belongs here is the durable path the
    console writes, ownership for the image's non-root user, and concrete public
    model ids so the next API start can assemble Direct Chat from the stored key.
    """

    compose = _compose()
    services = compose["services"]
    api = services["api"]
    environment = api["environment"]

    assert "AW_SECRETS__DEEPSEEK_API_KEY" not in environment
    assert environment["AW_KEY_FILE"] == ("/var/lib/agent-workbench/provider-key/key")
    assert environment["AW_MODEL__MAIN__MODEL_ID"] == "deepseek-chat"
    assert environment["AW_MODEL__COMPACT__MODEL_ID"] == "deepseek-chat"

    key_mount = next(
        mount
        for mount in api["volumes"]
        if mount["target"] == "/var/lib/agent-workbench/provider-key"
    )
    assert key_mount["type"] == "volume"
    assert key_mount["source"].endswith("provider_key_data")

    initializer = services["provider-key-init"]
    assert initializer["user"] == "0:0"
    assert key_mount["source"] in {mount["source"] for mount in initializer["volumes"]}
    assert "10001:10001" in " ".join(initializer["command"])
    assert api["depends_on"]["provider-key-init"]["condition"] == (
        "service_completed_successfully"
    )


def _launcher() -> str:
    """The Windows launcher's text, decoded under the rule it is written to."""

    return WINDOWS_LAUNCHER.read_bytes().decode("ascii")


def _launcher_commands() -> list[str]:
    """Its executable lines only.

    Every rule below is about what this file *runs*. Its comments quote the
    failures they exist to prevent -- including the exact command shapes those
    rules forbid -- so a naive substring search over the whole file matches the
    explanation and reports the opposite of the truth.
    """

    return [
        line.strip()
        for line in _launcher().splitlines()
        if not line.lstrip().lower().startswith("rem")
    ]


def test_windows_gets_a_launcher_for_the_whole_stack() -> None:
    """`dev.sh` is bash, so on Windows it is not a path that exists.

    `scripts/panel.cmd` covers the panel, which asks the machine for nothing
    but a Python. This covers the other half -- the system actually running --
    and asks it for nothing but Docker Desktop.
    """

    assert WINDOWS_LAUNCHER.is_file(), "scripts/stack.cmd is the Windows way in"
    raw = WINDOWS_LAUNCHER.read_bytes()

    # The two rules scripts/panel.cmd is held to, for the same reasons: cmd.exe
    # reads a batch file in the console OEM code page rather than UTF-8, and
    # `goto` into a label is where LF-only batch files are known to misbehave.
    raw.decode("ascii")
    assert b"\r\n" in raw
    assert re.search(rb"[^\r]\n", raw) is None, "every line ending must be CRLF"

    talkative = [
        line
        for line in raw.decode("ascii").splitlines()
        if line.lstrip().lower().startswith("rem") and any(ch in line for ch in "&|<>")
    ]
    assert not talkative, f"cmd.exe executes these rem lines: {talkative}"


def test_the_windows_launcher_never_builds_through_compose() -> None:
    """Compose cannot build this repository from a path with a Chinese name.

    Compose builds through buildx bake, which sets a gRPC header --
    x-docker-expose-session-sharedkey -- derived from the build context
    directory's own name. A non-ASCII name makes that header invalid, and the
    build dies before a single layer runs with a message naming neither the
    path nor the directory:

        failed to dial gRPC: ... header key "x-docker-expose-session-sharedkey"
        contains value with non-printable ASCII characters

    Measured 2026-09-01 on Docker 29.4.0, and it needs both halves: two or more
    services sharing one build context -- this topology has four -- together
    with a non-ASCII directory name. Four services under an ASCII name build.
    One service under a non-ASCII name builds. Four under a non-ASCII name
    never do, and COMPOSE_BAKE=false does not change that. Plain `docker build`
    does not take the bake path and is unaffected.

    A checkout under a Chinese directory name is the ordinary case for this
    project's readers on Windows, so the launcher builds first and starts
    second unconditionally, rather than behind a check that would have to
    re-derive the same rule.
    """

    through_compose = [
        line for line in _launcher_commands() if "compose" in line and "--build" in line
    ]
    assert not through_compose, through_compose
    assert "docker build -t agent-workbench:local ." in _launcher()


def test_the_windows_launcher_refuses_to_start_a_stale_image() -> None:
    """A failed build has to stop the run rather than hand Compose an old one.

    `compose up` without --build uses whatever agent-workbench:local already
    is, and after a build that failed that is the previous build. Without a
    guard between the two steps the launcher's happy path becomes "start the
    last code that compiled", which on screen is indistinguishable from
    success.
    """

    text = _launcher()
    between = text[
        text.index("docker build -t agent-workbench:local .") : text.index(
            "docker compose --profile demo up"
        )
    ]
    assert "errorlevel 1" in between, "a failed build reaches compose up unchallenged"


def test_the_windows_launcher_starts_the_profile_that_has_workers() -> None:
    """The default topology is a control plane, and it demonstrates nothing.

    Opening it shows Chat and an empty task list: no claim, no lease, no epoch,
    no fencing -- the part of this system that took the most work, and the part
    that only means anything under contention. The demo profile is what puts
    two Workers behind that page.
    """

    assert "--profile demo" in _launcher()


def test_the_windows_launcher_builds_the_tag_the_topology_runs() -> None:
    """Two files name this image, and a rename in one is silent in the other.

    Every building service declares a tag; the launcher builds a tag. Change
    either alone and `compose up` no longer sees the image that was just built
    -- and because each of those services also carries a `build:`, Compose
    quietly rebuilds it through the bake path this launcher exists to avoid,
    turning a rename into the failure two tests above describe.
    """

    services = _compose()["services"]
    assert isinstance(services, dict)
    built = {
        name: spec["image"]
        for name, spec in services.items()
        if isinstance(spec, dict) and "build" in spec and "image" in spec
    }
    assert built, "no service in this topology builds an image"

    text = _launcher()
    for name, image in built.items():
        assert f"docker build -t {image} ." in text, (
            f"service {name} runs {image}, which scripts/stack.cmd never builds"
        )


def test_the_windows_launcher_opens_the_port_the_topology_publishes() -> None:
    """The URL it opens is the one Compose maps, or the click lands nowhere.

    Both halves are asserted against the rendered topology rather than against
    each other: the host interface as well as the port, because publishing on
    0.0.0.0 would still satisfy a port-only check while quietly putting this
    console on every network the machine is joined to.
    """

    services = _compose()["services"]
    assert isinstance(services, dict)
    published = services["api"]["ports"][0]
    assert published["host_ip"] == "127.0.0.1"

    url = f"http://127.0.0.1:{published['published']}/ui/"
    # The `start` line specifically, not the file. This URL also appears in the
    # summary the launcher echoes, so a whole-file search stays green while the
    # click opens somewhere else -- which is exactly what it did when this
    # assertion was first written that way.
    launched = [
        line for line in _launcher_commands() if line.lower().startswith("start ")
    ]
    assert launched, "the launcher never opens a browser"
    assert any(url in line for line in launched), (
        f"the launcher should open {url}, it opens {launched}"
    )


def test_the_windows_launcher_says_the_stack_is_not_everything() -> None:
    """The pointer goes where the person actually is (ADR-102).

    This launcher's summary is the one moment somebody is looking at a window
    that could tell them. What it may not do is claim the stack is complete by
    saying nothing; a console read as broken was the cost of that silence.

    **The list this guards got shorter, and the assertion had to move with it.**
    It used to require the words "no embedding runtime", which was true while
    the image was built without the extra -- and became a lie the moment
    `Dockerfile` gained `--extra embedding`, with this test holding the lie in
    place. So it now names what is still absent rather than what was absent
    once: a topology of Linux containers cannot assemble the sandbox or
    computer use at all, and until somebody saves a provider key the Workers
    run synthetic handlers, which is the absence that looks least like one --
    a Task reaches `succeeded` having called no model and no tool.

    Asserted on the executable lines, because the paragraph of `rem` above them
    explains the same thing and would satisfy a whole-file search while the
    person running it saw nothing.
    """

    printed = " ".join(
        line for line in _launcher_commands() if line.lower().startswith("echo")
    )
    assert "System page" in printed
    assert "SYNTHETIC" in printed, "a synthetic Worker must not look like a real one"
    assert "Sandbox" in printed and "computer use" in printed


def test_the_windows_launcher_measures_the_machine_before_it_spends_its_time() -> None:
    """Four processes here load the retrieval models, and the machine may not
    have room for them.

    The cost of not asking is specific: tens of minutes of image build and
    weight download, and then `up --wait` timing out in swap -- which reads as
    "this project does not run" rather than as "Docker was given 8 GB". So the
    question is asked before the build, and after the subcommand dispatch, so
    that `down` on a small machine still stops the stack.

    The comparison must not go through `set /a`: MemTotal is a byte count and
    cmd's arithmetic is 32-bit signed, so every machine above ~2.1 GB overflows
    it -- silently, into a negative number that passes any `LSS` floor.
    """

    # Executable lines only, for the reason `_launcher_commands` gives: the
    # `rem` paragraph above this probe names `set /a` in order to explain why
    # it is not used, and a whole-file search finds that explanation and
    # reports the opposite of the truth. This test failed exactly that way
    # when it was written.
    run = "\n".join(_launcher_commands())
    assert "MemTotal" in run, "the launcher never asks how much memory Docker has"
    assert "set /a" not in run, "cmd arithmetic overflows on a byte count"

    dispatch = run.index('if /i "%~1"=="restart" goto :restart')
    assert run.index("MemTotal") > dispatch, (
        "the memory gate runs before the subcommand dispatch, so `down` on a "
        "small machine would refuse to stop the stack"
    )
    assert run.index("MemTotal") < run.index("docker build -t"), (
        "the machine is measured after the build, which is the whole cost the "
        "measurement exists to avoid"
    )


def _api_launcher() -> str:
    """The shell script the API container runs, as text."""

    return (ROOT / "docker" / "run-api-local.sh").read_text(encoding="utf-8")


def test_the_api_launcher_decides_web_search_by_probing_for_a_key() -> None:
    """The one switch that must not be set statically, and why (ADR-102).

    `research.enabled` without a provider key is a startup error rather than a
    degraded start, and the page that stores the key lives inside the process
    that would refuse to start. So a Compose file that wrote `true` here would
    make a fresh stack unbootable, and the person who saved a key on the
    settings page a minute earlier could never have got there.

    Asserted three ways because each half fails differently: the probe has to
    be *run*, the enablement has to sit behind it, and Compose has to pass an
    operator's own value through untouched.
    """

    launcher = _api_launcher()
    assert "docker/decide_web_search.py" in launcher
    enabling = launcher.index("AW_RESEARCH__ENABLED=true")
    probe = launcher.index("decide_web_search.py")
    assert probe < enabling, "research is enabled before anything looked for a key"
    # An explicit value from the operator survives: only unset/empty is decided.
    assert 'if [ -z "${AW_RESEARCH__ENABLED:-}" ]; then' in launcher


def test_compose_hands_the_operators_own_research_value_through() -> None:
    """Unset must arrive as empty rather than as `true` or `false`.

    `-` and not `:-`: Compose has no way to omit a key, so the launcher gets an
    empty string when the host said nothing, and unsets it before the process
    reads it. Written as a test because `${VAR:-false}` looks equivalent and
    would quietly disable the switch on every machine that never sets it.
    """

    api = _compose()["services"]["api"]
    assert api["environment"]["AW_RESEARCH__ENABLED"] == ""

    raw = COMPOSE_FILE.read_text(encoding="utf-8")
    assert 'AW_RESEARCH__ENABLED: "${AW_RESEARCH__ENABLED-}"' in raw


def test_both_workers_read_the_key_and_the_switches_the_api_reads() -> None:
    """One directory, one volume, three containers (ADR-101, ADR-103).

    A `--demo` Worker uses neither file, but a Worker is the process that
    registers the tools an envelope allows, so the day the flag comes off,
    "external_search is on" has to mean the same thing in every container.
    """

    services = _compose()["services"]
    api_key_file = services["api"]["environment"]["AW_KEY_FILE"]
    for name in ("task-worker", "task-worker-b"):
        worker = services[name]
        assert worker["environment"]["AW_KEY_FILE"] == api_key_file, name
        mount = next(
            m
            for m in worker["volumes"]
            if m["target"] == "/var/lib/agent-workbench/provider-key"
        )
        assert mount["source"].endswith("provider_key_data"), name
        assert worker["depends_on"]["provider-key-init"]["condition"] == (
            "service_completed_successfully"
        ), name


def test_the_windows_launcher_can_restart_just_the_processes_that_read_config() -> None:
    """A saved key or a flipped switch is read at the next start of three
    processes, and `down` followed by a fresh start would rebuild the image."""

    restarts = [
        line for line in _launcher_commands() if "compose" in line and "restart" in line
    ]
    assert restarts == [
        "docker compose --profile demo restart api task-worker task-worker-b"
    ], restarts
    assert 'if /i "%~1"=="restart" goto :restart' in _launcher_commands()
    # Listed where the other subcommands are, so a person finds it.
    assert "scripts\\stack.cmd restart" in _launcher()


#: Every service that builds a real embedder, and therefore loads BGE-M3 dense,
#: BGE-M3 sparse and the reranker into its own address space.
RETRIEVING_SERVICES = ("api", "task-worker", "task-worker-b", "ingestion-worker")


def test_the_stack_names_the_profile_it_runs() -> None:
    """A deployment that picked no profile is not a deployment that chose.

    No service used to set `AW_CONFIG_FILE`, so the whole topology loaded
    `config.default.toml` -- which ships no MCP servers, no triage, no Code and
    no delegation. Those are reasonable defaults and were never a decision
    about this stack, which is how eleven healthy containers came to present a
    console with none of that in it.
    """

    services = _compose()["services"]
    assert isinstance(services, dict)

    named = {
        name: (service.get("environment") or {}).get("AW_CONFIG_FILE")
        for name, service in services.items()
        if name in RETRIEVING_SERVICES
    }
    assert set(named) == set(RETRIEVING_SERVICES), named
    assert set(named.values()) == {"/app/config/config.compose-local.toml"}, named

    # The path is inside the image, so the only way this test can be green
    # against a file that is not there is if the file is not there.
    profile = ROOT / "config" / "config.compose-local.toml"
    assert profile.is_file(), "the profile every container names does not exist"
    parsed = tomllib.loads(profile.read_text(encoding="utf-8"))
    assert parsed["optional_labs"]["mcp_adapter"] is True
    assert [server["alias"] for server in parsed["mcp"]["servers"]] == ["word", "web"]
    # The one line that would take the API down if it were copied across from
    # `config.demo-local.toml`: `SandboxSession.open` is fail-fast, and nothing
    # in this topology can answer a sandbox probe.
    assert parsed["code"]["sandbox_enabled"] is False
    assert "sandbox" not in parsed
    # `config.demo-local.toml` points Qdrant at loopback because its processes
    # are on the host. The shipped default is already the service name here, so
    # an override copied over would break retrieval with a timeout.
    assert "qdrant" not in parsed


def test_every_process_that_retrieves_gets_the_weights_it_needs() -> None:
    """Four processes, one cache, and a one-shot that fills it first.

    The ordering is not an optimisation. The sparse arm checks the cache for
    BGE-M3's trained lexical head *before* it builds the model and raises when
    it is absent -- because FlagEmbedding's own behaviour there is to
    substitute a randomly initialised projection and carry on. So a cold cache
    does not make the first start slow; it makes all four of these exit.
    """

    services = _compose()["services"]
    assert isinstance(services, dict)

    for name in RETRIEVING_SERVICES:
        service = services[name]
        environment = service.get("environment") or {}
        assert environment.get("HF_HOME") == "/var/lib/agent-workbench/hf-cache", name
        mounts = [
            mount
            for mount in service.get("volumes", [])
            if mount.get("target") == "/var/lib/agent-workbench/hf-cache"
        ]
        assert len(mounts) == 1, name
        assert mounts[0]["source"].endswith("hf_cache"), name
        assert service["depends_on"]["weights-init"]["condition"] == (
            "service_completed_successfully"
        ), name

    # Not baked into the image: gigabytes in a layer are re-pulled on every
    # rebuild, and the build machine would need to reach Hugging Face even with
    # a warm volume sitting beside it.
    assert "fetch_weights.py" in " ".join(services["weights-init"]["command"])


def test_the_ingestion_worker_writes_vectors_that_mean_something() -> None:
    """`--demo` swapped the embedder for a hash of the chunk text.

    Vectors of the right width, in which similar sentences are not near each
    other -- and nothing said so: the upload completed, the document rendered
    as searchable, and every query came back wrong. With a real embedder in the
    image there is no reason left to write those.
    """

    services = _compose()["services"]
    assert isinstance(services, dict)
    command = services["ingestion-worker"]["command"]
    assert "--demo" not in command, command
    # It stays the one service allowed to create the collection and the alias.
    environment = services["ingestion-worker"].get("environment") or {}
    assert environment.get("AW_QDRANT__ALLOW_LOCAL_BOOTSTRAP") == "true"


def test_the_task_worker_starts_its_tools_before_it_freezes_its_catalogue() -> None:
    """A Worker reads its MCP catalogue once, at startup, and never again.

    Discovery failure is fail-soft -- one `mcp_connection_failed` line and the
    process continues -- so a Worker that starts before its servers is not a
    Worker that retries. It is a healthy Worker permanently missing the tools
    it exists for.

    Health is not enough to gate on: both servers answer `/health` with `ok`
    from the moment uvicorn binds, before the MCP application can list a tool.
    So the gate is the same real-client probe `scripts/dev.sh` uses.
    """

    # Executable lines only. The header comment explains the ordering this
    # asserts, naming every command in it, so a whole-file search would be
    # satisfied by the explanation alone.
    entrypoint = "\n".join(
        line
        for line in (ROOT / "docker" / "run-task-worker-local.sh")
        .read_text(encoding="utf-8")
        .splitlines()
        if not line.lstrip().startswith("#")
    )
    probe = entrypoint.index("smoke_mcp_server.py")
    assert "agent-word-mcp" in entrypoint and "agent-web-mcp" in entrypoint
    assert entrypoint.index("agent-word-mcp") < probe
    assert probe < entrypoint.index("exec agent-task-worker")
    assert "--expect-tool render_document" in entrypoint
    assert "--expect-tool fetch_page" in entrypoint

    # It must fall back rather than exit. `up -d --wait` reads an exiting
    # container as the stack failing to come up -- and having typed no provider
    # key yet is the ordinary state of a first run, not a failure.
    assert "exec agent-task-worker --demo" in entrypoint
    assert "SYNTHETIC" in entrypoint

    services = _compose()["services"]
    assert isinstance(services, dict)
    for name in ("task-worker", "task-worker-b"):
        assert "run-task-worker-local.sh" in " ".join(services[name]["command"]), name


def test_every_application_service_keeps_the_hardening() -> None:
    """The anchor is easy to leave off a new service, and nothing noticed.

    Read-only root, no new privileges and an empty capability set are what
    make it defensible to run this on a laptop at all. A service that quietly
    skips them is not visible in a diff of `compose.yaml`, because what is
    missing there is a line nobody wrote.
    """

    services = _compose()["services"]
    assert isinstance(services, dict)

    for name, service in services.items():
        if service.get("image") != "agent-workbench:local":
            continue
        if name in {"otel-init", "provider-key-init"}:
            # Both exist to chown a volume for the non-root user, which is the
            # one job that needs to run as root and write outside a volume.
            continue
        assert service.get("read_only") is True, name
        assert "no-new-privileges:true" in service.get("security_opt", []), name
        assert service.get("cap_drop") == ["ALL"], name
