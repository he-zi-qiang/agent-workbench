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


#: The one `docker build` that produces the tag the topology runs. Build
#: arguments may sit between `build` and `-t` (ADR-0109 added one); the tag
#: and the bare `.` context are what the rules below are about.
_IMAGE_BUILD = re.compile(
    r"^docker build (?P<args>(?:--build-arg \S+ )*)-t (?P<tag>\S+) \.$"
)


def _image_build_line() -> str:
    """The launcher's image build command, or a failure naming what was found."""

    matches = [line for line in _launcher_commands() if _IMAGE_BUILD.match(line)]
    assert len(matches) == 1, matches
    return matches[0]


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
    match = _IMAGE_BUILD.match(_image_build_line())
    assert match is not None
    assert match.group("tag") == "agent-workbench:local"


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
        text.index(_image_build_line()) : text.index("docker compose --profile demo up")
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

    match = _IMAGE_BUILD.match(_image_build_line())
    assert match is not None
    for name, image in built.items():
        assert match.group("tag") == image, (
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
    once -- and the list moved again with ADR-0107 and ADR-0108: the sandbox
    is present (and its one failure mode, an image the broker could not pull,
    is named), computer use is a second launcher on the host rather than an
    absence, and until somebody saves a provider key the Workers run synthetic
    handlers, which is the absence that looks least like one -- a Task reaches
    `succeeded` having called no model and no tool.

    Asserted on the executable lines, because the paragraph of `rem` above them
    explains the same thing and would satisfy a whole-file search while the
    person running it saw nothing.
    """

    printed = " ".join(
        line for line in _launcher_commands() if line.lower().startswith("echo")
    )
    assert "System page" in printed
    assert "SYNTHETIC" in printed, "a synthetic Worker must not look like a real one"
    # Since ADR-0107 the sandbox is present, and its one failure mode is named;
    # since ADR-0108 computer use is a second launcher on the host, not an
    # absence, and the summary says which file.
    assert "Sandbox" in printed and "sandbox container" in printed
    assert "computer.cmd" in printed
    assert "absent from any container topology" not in printed


def test_the_windows_launcher_measures_the_machine_before_it_spends_its_time() -> None:
    """One process here loads the retrieval models (ADR-0106), and the machine
    may not have room for it.

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
    # The two lines are the one measured figure (12 GB for one model-holding
    # process) and that figure plus a stated allowance; the pre-ADR-0106
    # floors were four times that and are gone.
    assert "GEQ 16 goto :memory_ok" in run and "GEQ 12 goto :memory_tight" in run
    assert "GEQ 51" not in run and "GEQ 29" not in run

    dispatch = run.index('if /i "%~1"=="restart" goto :restart')
    assert run.index("MemTotal") > dispatch, (
        "the memory gate runs before the subcommand dispatch, so `down` on a "
        "small machine would refuse to stop the stack"
    )
    assert run.index("MemTotal") < run.index("docker build "), (
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
    # The sandbox broker joins the list (ADR-0107): it picks its image at start,
    # so an image built by `sandbox-image` is seen only after this. The encoder
    # is deliberately absent -- restarting it reloads three models.
    assert restarts == [
        "docker compose --profile demo restart sandbox api task-worker task-worker-b"
    ], restarts
    assert not any("encoder" in line for line in restarts)
    assert 'if /i "%~1"=="restart" goto :restart' in _launcher_commands()
    # Listed where the other subcommands are, so a person finds it.
    assert "scripts\\stack.cmd restart" in _launcher()


#: The processes that retrieve, and therefore used to load BGE-M3 dense, BGE-M3
#: sparse and the reranker each into its own address space. Since ADR-0106
#: they ask the encoder instead, and this tuple names what must *not* hold the
#: weights any more.
RETRIEVING_SERVICES = ("api", "task-worker", "task-worker-b", "ingestion-worker")
#: The one process that does.
ENCODER = "encoder"
HF_CACHE = "/var/lib/agent-workbench/hf-cache"


def _mounts(service: dict[str, object], target: str) -> list[dict[str, object]]:
    volumes = service.get("volumes", [])
    assert isinstance(volumes, list)
    return [m for m in volumes if isinstance(m, dict) and m.get("target") == target]


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
    assert [server["alias"] for server in parsed["mcp"]["servers"]] == [
        "word",
        "web",
        # ADR-0113. Listed rather than counted, because each alias widens every
        # Task submitted under this profile by its own tools, and a test that
        # only counted them would let a rename through.
        "browser",
    ]
    # The one line that would take the API down if it were copied across from
    # `config.demo-local.toml`: `SandboxSession.open` is fail-fast, and nothing
    # in this topology can answer a sandbox probe.
    assert parsed["code"]["sandbox_enabled"] is False
    assert "sandbox" not in parsed
    # `config.demo-local.toml` points Qdrant at loopback because its processes
    # are on the host. The shipped default is already the service name here, so
    # an override copied over would break retrieval with a timeout.
    assert "qdrant" not in parsed


def test_the_stack_names_the_profile_for_the_encoder_too() -> None:
    """It reads the same `[rag.embedding]` the other four read, which is what
    keeps "the model the encoder loads" and "the model they expect" one
    declaration (ADR-0106 §3.2)."""

    services = _compose()["services"]
    assert isinstance(services, dict)
    environment = services[ENCODER].get("environment") or {}
    assert environment.get("AW_CONFIG_FILE") == "/app/config/config.compose-local.toml"


def test_only_the_encoder_gets_the_weights_and_everyone_else_waits_for_it() -> None:
    """One cache, one process, four clients (ADR-0106).

    The ordering half is not an optimisation: the sparse arm checks the cache
    for BGE-M3's trained lexical head *before* it builds the model and raises
    when it is absent, so a cold cache does not make the encoder slow, it
    makes it exit. The other half is the whole point of the ADR: a service
    that still mounted the cache would be a service that could still load the
    weights, and the memory floor the launcher checks assumes exactly one does.
    """

    services = _compose()["services"]
    assert isinstance(services, dict)

    encoder = services[ENCODER]
    assert (encoder.get("environment") or {}).get("HF_HOME") == HF_CACHE
    mounts = _mounts(encoder, HF_CACHE)
    assert len(mounts) == 1 and str(mounts[0]["source"]).endswith("hf_cache")
    assert encoder["depends_on"]["weights-init"]["condition"] == (
        "service_completed_successfully"
    )
    assert "fetch_weights.py" in " ".join(services["weights-init"]["command"])

    for name in RETRIEVING_SERVICES:
        service = services[name]
        assert _mounts(service, HF_CACHE) == [], f"{name} still mounts the weights"
        assert "weights-init" not in service["depends_on"], name
        assert service["depends_on"][ENCODER]["condition"] == "service_healthy", name


def test_the_encoder_is_reached_by_name_and_published_nowhere() -> None:
    """Its own container, bound on its interface the way Qdrant is, and the
    only project server allowed to be: it is not an MCP server and holds no
    identity (ADR-0106 §3.3). No port reaches the host."""

    services = _compose()["services"]
    assert isinstance(services, dict)
    command = " ".join(services[ENCODER]["command"])
    assert command.startswith("agent-encoder")
    assert "--host 0.0.0.0" in command
    assert "ports" not in services[ENCODER]
    assert services[ENCODER]["healthcheck"]["test"][-1].count("/health") == 1


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


# --- the sandbox broker (ADR-0107) --------------------------------------------

SOCKET = "/var/run/docker.sock"


def test_exactly_one_service_holds_the_docker_socket_and_it_is_the_broker() -> None:
    """The trade ADR-0105 refused to make by mount, made by topology instead.

    A socket in the API's container is root on the VM for anything that gets
    into a process that also holds a provider key, a database and every
    workspace. The broker holds none of those -- no key volume, no artifact
    volume, no configuration -- so what a compromise of it buys is the daemon
    and nothing else, which is what the native path already hands the sandbox
    server. Asserted as *exactly one*, because the failure this guards is a
    second mount added to a service that looked like it needed one.
    """

    services = _compose()["services"]
    assert isinstance(services, dict)
    holders = sorted(name for name, spec in services.items() if _mounts(spec, SOCKET))
    assert holders == ["sandbox"], holders

    broker = services["sandbox"]
    assert "run-sandbox-local.sh" in " ".join(broker["command"])
    assert _mounts(broker, "/var/lib/agent-workbench/provider-key") == []
    assert _mounts(broker, "/var/lib/agent-workbench/artifacts") == []
    assert "AW_CONFIG_FILE" not in (broker.get("environment") or {})
    assert "AW_DATABASE__DSN" not in (broker.get("environment") or {})
    # Root, because the socket is root-owned; the hardening anchor still
    # applies (`test_every_application_service_keeps_the_hardening` covers it).
    assert broker.get("user") == "0:0"
    assert "ports" not in broker


def test_the_clients_of_the_broker_reach_it_through_a_loopback_tunnel() -> None:
    """Every guard on that path -- the settings validator, the MCP SDK's Host
    check, the server's own `--host` choice list -- is about a loopback
    address, and a tunnel whose two ends are both loopback keeps every one of
    them true (docker/loopback_proxy.py). The tunnel has to be up before the
    process that dials it, and the probe has to be behind the tunnel."""

    for launcher in (
        ROOT / "docker" / "run-api-local.sh",
        ROOT / "docker" / "run-task-worker-local.sh",
    ):
        text = "\n".join(
            line
            for line in launcher.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        tunnel = text.index(
            'LOCAL_PROXY_UPSTREAM_HOST="${SANDBOX_UPSTREAM_HOST:-sandbox}"'
        )
        probe = text.index("decide_sandbox.py")
        started = text.index(
            "agent-api" if "api" in launcher.name else "exec agent-task-worker"
        )
        assert tunnel < probe < started, launcher.name
        assert "LOCAL_PROXY_LISTEN_HOST=127.0.0.1" in text, launcher.name
        assert (
            "LOCAL_PROXY_PORT=8766" in text
            or 'LOCAL_PROXY_PORT="$SANDBOX_PORT"' in text
        )

    services = _compose()["services"]
    assert isinstance(services, dict)
    for name in ("api", "task-worker", "task-worker-b"):
        assert services[name]["depends_on"]["sandbox"]["condition"] == "service_healthy"


def test_the_launchers_decide_the_sandbox_and_leave_an_operators_value_alone() -> None:
    """On without a broker that answers is a startup error by design (ADR-057),
    so the profile leaves it off and the launcher turns it on when the
    runtime answers -- the shape web search already has (ADR-102 §3)."""

    api = _api_launcher()
    assert (
        'if [ -z "${AW_CODE__SANDBOX_ENABLED:-}" ] && [ -z "${AW_SANDBOX__ENABLED:-}" ]'
        in api
    )
    assert api.index("decide_sandbox.py") < api.index("AW_CODE__SANDBOX_ENABLED=true")
    worker = (ROOT / "docker" / "run-task-worker-local.sh").read_text(encoding="utf-8")
    assert 'if [ -z "${AW_SANDBOX__ENABLED:-}" ]; then' in worker
    assert worker.index("decide_sandbox.py") < worker.index("AW_SANDBOX__ENABLED=true")

    services = _compose()["services"]
    assert isinstance(services, dict)
    assert services["api"]["environment"]["AW_CODE__SANDBOX_ENABLED"] == ""
    assert services["api"]["environment"]["AW_SANDBOX__ENABLED"] == ""
    for name in ("task-worker", "task-worker-b"):
        assert services[name]["environment"]["AW_SANDBOX__ENABLED"] == "", name


def test_the_image_carries_the_docker_cli_for_the_broker() -> None:
    """Copied from Docker's own CLI image rather than installed from an apt
    repository; inert in every container that has no socket."""

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY --from=docker:" in dockerfile
    assert "/usr/local/bin/docker /usr/local/bin/docker" in dockerfile


def test_the_windows_launcher_builds_the_pdf_sandbox_image_without_compose() -> None:
    builds = [line for line in _launcher_commands() if line.startswith("docker build")]
    assert any("sandbox-pdf.Dockerfile" in line for line in builds), builds
    assert 'if /i "%~1"=="sandbox-image" goto :sandbox_image' in _launcher_commands()


# --- computer use on the host (ADR-0108) --------------------------------------


def test_the_api_can_name_the_host_and_tunnels_the_screen_servers_session() -> None:
    """No container can reach the desktop, so the screen server runs on the
    host; the API reads its one read-only route through a loopback tunnel to
    the host's name, and `host-gateway` makes that name resolve on a Linux
    engine as well as under Docker Desktop."""

    raw = COMPOSE_FILE.read_text(encoding="utf-8")
    assert '"host.docker.internal:host-gateway"' in raw
    api = _compose()["services"]["api"]
    assert "host.docker.internal" in str(api.get("extra_hosts"))

    launcher = _api_launcher()
    assert (
        'LOCAL_PROXY_UPSTREAM_HOST="${COMPUTER_UPSTREAM_HOST:-host.docker.internal}"'
        in launcher
    )
    assert "LOCAL_PROXY_PORT=8768" in launcher


# --- ADR-0109: the two things a Windows console still could not do ----------


def test_the_windows_launcher_builds_the_image_that_can_lay_a_document_out() -> None:
    """`stack.cmd` passes the fidelity build argument; the Dockerfile default stays off.

    ADR-0045 left LibreOffice out of the default image and said why: a
    several-hundred-megabyte download that has failed mid-build, on an image
    that is not broken without it. That default is right for CI and wrong for
    the one launcher whose job since ADR-0105 is to assemble everything a
    container can -- a console that shows a Word report as extracted text
    reads as a console that cannot preview Word, and one did.
    """

    commands = _launcher_commands()
    builds = [
        line for line in commands if line.startswith("docker build ") and " ." in line
    ]
    # Two, and the count is asserted rather than the first match taken: a third
    # build appearing here is a third multi-minute step in the one path whose
    # whole promise is "double-click and wait once", and it should have to be
    # written into a test before it is written into the launcher.
    assert len(builds) == 2, builds
    assert "--build-arg WITH_FIDELITY_PREVIEW=%FIDELITY%" in builds[0]
    assert 'set "FIDELITY=1"' in commands
    # The lighter image is a word somebody types, not a second launcher.
    assert 'if /i "%~1"=="lite" set "FIDELITY=0"' in commands

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG WITH_FIDELITY_PREVIEW=0" in dockerfile, (
        "the Dockerfile default moved; the launcher's argument is the decision"
    )


def test_the_windows_launcher_builds_the_browser_image_in_order() -> None:
    """The second image, and the order it has to come in (ADR-0113 §3.5b).

    `docker/browser.Dockerfile` is `FROM agent-workbench:local`, so building it
    first gets either a stale base or no base at all. Compose cannot enforce
    that ordering here because this is deliberately not a compose `build:` --
    on a checkout whose path contains non-ASCII characters `compose build`
    fails with an error that never mentions the path, which is the failure this
    project has already been bitten by.

    The reason it is a second image at all is size: `api`, both Workers,
    `ingestion`, `sandbox`, `encoder` and `browser-egress` share the base one,
    and none of them can start a browser.
    """

    commands = _launcher_commands()
    builds = [
        line for line in commands if line.startswith("docker build ") and " ." in line
    ]
    base, browser = builds
    assert "-t agent-workbench:local" in base
    assert "-f docker\\browser.Dockerfile" in browser
    assert "-t agent-workbench-browser:local" in browser
    assert "--build-arg BASE_IMAGE=agent-workbench:local" in browser, (
        "the derived image must name what it derives from, or a rebuild of the "
        "base leaves this one silently pinned to an older layer"
    )

    compose = _compose()
    service = compose["services"]["browser"]
    assert service["image"] == "agent-workbench-browser:local"
    assert "build" not in service, (
        "a compose `build:` here is what fails on a CJK path; the launcher "
        "builds this image and compose only names it"
    )


def test_the_api_alone_can_write_one_host_folder_and_the_picker_opens_there() -> None:
    """A coding session edits real files, so the topology has to hand it a folder.

    Before this the picker opened at `/app`, the image's read-only tree:
    every directory was choosable and the first `project_write` failed with a
    sentence about a read-only filesystem. Three facts have to agree -- the
    mount in `compose.yaml`, the root the profile names, and the launcher
    creating the folder -- and this pins each to the same path.
    """

    services = _compose()["services"]
    assert isinstance(services, dict)

    def bind_targets(name: str) -> set[str]:
        spec = services[name]
        assert isinstance(spec, dict)
        volumes = spec.get("volumes", [])
        assert isinstance(volumes, list)
        return {
            str(volume["target"])
            for volume in volumes
            if isinstance(volume, dict) and volume.get("type") == "bind"
        }

    assert "/projects" in bind_targets("api")
    # Only the process that runs Code turns. A Worker holding a writable host
    # folder would be a second write path into the user's disk for no reader.
    for name in ("task-worker", "task-worker-b", "ingestion-worker", "encoder"):
        assert "/projects" not in bind_targets(name), name

    profile = tomllib.loads(
        (ROOT / "config/config.compose-local.toml").read_text(encoding="utf-8")
    )
    assert profile["code"]["projects_root"] == "/projects"

    assert 'if not exist "var\\projects" mkdir "var\\projects"' in _launcher_commands()
    # Outside the `AW_` namespace: settings reject unknown `AW_*` variables,
    # and this one is Compose's, never the application's.
    text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "${AGENT_WORKBENCH_PROJECTS_DIR:-./var/projects}:/projects" in text


# --- The browser's way out is a shape, not a flag (ADR-0113 §3.3) ----------


def test_the_browser_container_has_no_default_route() -> None:
    """The claim the whole design rests on, asserted rather than described.

    `--proxy-server` is a command-line argument, and an argument can be
    omitted by anything that gets to run code in the container. A missing
    default route cannot be. So the browser is on one network, that network is
    `internal`, and the only thing on it that can leave is the guard.
    """

    compose = _compose()
    services = compose["services"]
    networks = compose["networks"]
    assert isinstance(services, dict)
    assert isinstance(networks, dict)

    browser = services["browser"]
    attached = sorted((browser.get("networks") or {}).keys())
    assert attached == ["browser"], attached
    assert networks["browser"]["internal"] is True


def test_the_guard_is_the_only_way_from_that_network_to_the_outside() -> None:
    """Exactly one service bridges the internal network and the default one."""

    services = _compose()["services"]
    assert isinstance(services, dict)

    bridging = sorted(
        name
        for name, service in services.items()
        if "browser" in (service.get("networks") or {})
        and "default" in (service.get("networks") or {})
        # The API and the Workers are on both because they *call* the browser;
        # what matters is which of them the browser itself can reach, and it
        # reaches only what is on its network with it.
        and name not in {"api", "task-worker", "task-worker-b"}
    )
    assert bridging == ["browser-egress"], bridging


def test_the_browser_keeps_its_own_sandbox() -> None:
    """ADR-0113 §3.5: the hardening stays, and the seccomp profile is what moves.

    If this profile is ever dropped, Chromium aborts at start rather than
    running unsandboxed -- but it would abort in a container whose `cap_drop`
    somebody had probably relaxed to "fix" it, which is the trade this test
    exists to keep visible.
    """

    services = _compose()["services"]
    assert isinstance(services, dict)
    browser = services["browser"]

    options = browser.get("security_opt", [])
    assert "no-new-privileges:true" in options
    assert any("chromium-seccomp.json" in str(option) for option in options), options
    assert browser.get("cap_drop") == ["ALL"]
    assert browser.get("read_only") is True


def test_the_browser_sees_the_workspace_read_only() -> None:
    """It opens what a session produced; it has no business writing there."""

    services = _compose()["services"]
    assert isinstance(services, dict)
    mounts = services["browser"].get("volumes", [])
    workspace = [mount for mount in mounts if mount.get("target") == "/workspace"]
    assert len(workspace) == 1, mounts
    assert workspace[0].get("read_only") is True
    assert workspace[0].get("source") == "artifact_data"


def test_neither_browser_service_holds_anything_worth_stealing() -> None:
    """No key volume, no database, no Docker socket -- stated as an assertion.

    The ADR's argument for `--no-sandbox` being unnecessary, and for the blast
    radius being acceptable if Chromium is ever exploited, both depend on this
    container holding nothing. That is only true while nobody adds a mount.
    """

    services = _compose()["services"]
    assert isinstance(services, dict)

    for name in ("browser", "browser-egress"):
        service = services[name]
        sources = {mount.get("source") for mount in service.get("volumes", [])}
        assert "/var/run/docker.sock" not in sources, name
        assert "provider_key_data" not in sources, name
        environment = service.get("environment") or {}
        assert not any("DSN" in key for key in environment), name
        assert not any("SECRET" in key.upper() for key in environment), name


def test_the_callers_of_the_browser_are_on_its_network() -> None:
    """A tunnel to a service on a network you are not on reaches nothing."""

    services = _compose()["services"]
    assert isinstance(services, dict)
    for name in ("api", "task-worker", "task-worker-b"):
        attached = (services[name].get("networks") or {}).keys()
        assert "browser" in attached, name
        # And still on the default one, or they lose the database: a service
        # that names any network is no longer on the implicit one.
        assert "default" in attached, name


def test_the_runner_holds_the_project_folder_and_nothing_else() -> None:
    """ADR-0115 §3.2, the shape F-37 asked for: a container that holds no key
    and only runs shell commands.

    Every absence is asserted because every absence is the point. The API's
    container holds a provider key, a database address and every workspace;
    ADR-0109 §3.3 refused a shell there for exactly that reason. This one
    holds the host folder the API mounts -- read-write, because `project_run`
    runs the project's own formatter and tests -- and nothing else.
    """

    services = _compose()["services"]
    assert isinstance(services, dict)
    runner = services["runner"]
    assert "run-runner-local.sh" in " ".join(runner["command"])
    environment = runner.get("environment") or {}
    assert "AW_CONFIG_FILE" not in environment
    assert "AW_DATABASE__DSN" not in environment
    assert "AW_KEY_FILE" not in environment
    assert _mounts(runner, "/var/lib/agent-workbench/provider-key") == []
    assert _mounts(runner, "/var/lib/agent-workbench/artifacts") == []
    assert _mounts(runner, SOCKET) == []
    assert "ports" not in runner
    assert runner.get("read_only") is True
    assert "ALL" in runner["cap_drop"]

    projects = _mounts(runner, "/projects")
    assert len(projects) == 1, projects
    assert projects[0].get("read_only") is not True
    # The same host folder the API writes, or `cwd` would name a directory
    # that exists on one side and not the other.
    assert projects[0]["source"] == _mounts(services["api"], "/projects")[0]["source"]


def test_only_the_api_shares_the_runners_network() -> None:
    """Who else is reachable from a command the model wrote: the API's console
    proxy, as a shell on the host could reach 127.0.0.1:8000 -- and not
    PostgreSQL (trust auth, no password), not Qdrant, not the encoder, not the
    sandbox broker. Not `internal`, on purpose: a command may need the
    network the way it would on the user's machine, and the runner holds
    nothing whose theft a missing route would prevent."""

    compose = _compose()
    services = compose["services"]
    networks = compose["networks"]
    assert isinstance(services, dict)
    assert isinstance(networks, dict)
    on_runner = sorted(
        name
        for name, service in services.items()
        if "runner" in (service.get("networks") or {})
    )
    assert on_runner == ["api", "runner"], on_runner
    assert sorted((services["runner"].get("networks") or {}).keys()) == ["runner"]
    assert networks["runner"].get("internal") is not True


def test_the_browser_sees_the_projects_folder_read_only_at_the_apis_path() -> None:
    """ADR-0115 §3.3. The API turns a project turn's `workspace_path` into a
    `file://` URL under *its* view of the project directory, and the browser
    opens that URL under *its* view -- so the two mounts must share a source
    and a target, and the browser's must not be able to write."""

    services = _compose()["services"]
    assert isinstance(services, dict)
    projects = _mounts(services["browser"], "/projects")
    assert len(projects) == 1, projects
    assert projects[0].get("read_only") is True
    assert projects[0]["source"] == _mounts(services["api"], "/projects")[0]["source"]


def test_the_api_launcher_decides_the_runner_and_the_browser_by_probing() -> None:
    """Both slots are fail-fast, so neither switch is set in the profile;
    the launcher tunnels to each container, probes it with the Task Worker's
    own MCP smoke test, and exports the switch only when the tool is
    advertised. Tunnel before probe before `agent-api`, and an operator's
    explicit value left alone -- the sandbox's arrangement, twice more."""

    text = "\n".join(
        line
        for line in (ROOT / "docker" / "run-api-local.sh")
        .read_text(encoding="utf-8")
        .splitlines()
        if not line.lstrip().startswith("#")
    )
    started = text.index("agent-api --web-dir")
    for upstream, tool, switch in (
        ("browser", "browser_open", "AW_CODE__BROWSER_ENABLED"),
        ("runner", "run_command", "AW_RUNNER__ENABLED"),
    ):
        tunnel = text.index(
            f'LOCAL_PROXY_UPSTREAM_HOST="${{{upstream.upper()}_UPSTREAM_HOST:-{upstream}}}"'
        )
        probe = text.index(f"--expect-tool {tool}")
        assert tunnel < probe < started, upstream
        assert f'if [ -z "${{{switch}:-}}" ]' in text, switch
        assert f"{switch}=true" in text, switch
        assert text.index(f"--expect-tool {tool}") < text.index(f"{switch}=true"), (
            switch
        )

    services = _compose()["services"]
    assert isinstance(services, dict)
    assert services["api"]["environment"]["AW_RUNNER__ENABLED"] == ""
    assert services["api"]["environment"]["AW_CODE__BROWSER_ENABLED"] == ""
    assert services["api"]["depends_on"]["runner"]["condition"] == "service_healthy"
    assert services["api"]["depends_on"]["browser"]["condition"] == "service_healthy"


def test_every_loopback_healthcheck_probes_the_port_its_own_process_listens_on() -> (
    None
):
    """Measured 2026-09-12: a `sed` that moved the runner off 8769 rewrote the
    encoder's healthcheck too -- the encoder listens on 8769 and its check went
    looking on 8774, so it was `unhealthy` for ever, `compose up --wait` timed
    out, and the API (which depends on it) was never started. Nothing failed
    loudly: the encoder was up, warm and answering on the port nobody probed.

    So the port a healthcheck probes is derived from the same place the
    process gets it, and asserted equal: the encoder's `--port` argument, the
    runner's `DEFAULT_PORT`, the sandbox's 8766, the browser's 8780 server
    port. A check that probes a port the process does not bind is a stack
    that cannot come up, and it looks exactly like a slow model load.
    """

    import re

    from agent_workbench.apps.runner_mcp.main import DEFAULT_PORT as RUNNER_PORT

    services = _compose()["services"]
    assert isinstance(services, dict)

    def probed(name: str) -> int:
        test = " ".join(services[name]["healthcheck"]["test"])
        match = re.search(r"127\.0\.0\.1:(\d+)/health", test)
        assert match, f"{name}'s healthcheck does not probe a loopback /health: {test}"
        return int(match.group(1))

    encoder_command = services["encoder"]["command"]
    listens = int(encoder_command[encoder_command.index("--port") + 1])
    assert probed("encoder") == listens
    assert probed("runner") == RUNNER_PORT
    assert probed("sandbox") == 8766
    # The browser publishes 8773 through its tunnel and serves on 8780 itself;
    # the check runs inside the container, so it is the server port.
    assert probed("browser") == 8780


def test_the_image_carries_node_for_the_runner_and_nothing_to_install_with() -> None:
    """ADR-0115, after the first turn that had the runner: it looked for a JS
    engine in `/usr/bin`, found none, and reported it could not run the
    project's own `check_level.js`. The binary comes from the same pinned
    `web-build` stage the console is built with, so no new base is pulled;
    npm stays out, on purpose -- a read-only container with a tmpfs home is
    not where `npm install` should be spending a turn."""

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY --from=web-build /usr/local/bin/node /usr/local/bin/node" in dockerfile
    assert "/usr/local/lib/node_modules" not in dockerfile


# --- the dependency layer outlives a change to the code (2026-09-13) ----------


def _dockerfile_instructions(path: Path) -> list[str]:
    """One string per instruction: comments dropped, continuations joined.

    Read in order rather than searched, because what this guards is an
    ordering -- and every line of it would still be in the file, and still
    satisfy a substring search, after somebody moved a `COPY` back above the
    sync.
    """

    instructions: list[str] = []
    pending = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        instructions.append(" ".join((pending + line).split()))
        pending = ""
    return instructions


def _context_sources(instruction: str) -> list[str]:
    """What a `COPY` takes from the build context; nothing, for `--from=`."""

    if not instruction.startswith("COPY ") or "--from=" in instruction:
        return []
    arguments = [
        token for token in instruction.split()[1:] if not token.startswith("--")
    ]
    return arguments[:-1]


def test_a_change_to_the_code_or_the_console_reuses_the_dependency_layer() -> None:
    """Until 2026-09-13 the image ran one `uv sync` after every `COPY`, so a
    rebuild whose only change was in `web/` downloaded all 175 packages of the
    `embedding` extra again -- `Prepared 175 packages in 4m 44s`, then 230s
    exporting an 11.5 GB layer, 5.2 GB of it uv's own download cache.

    What fixed it is where two lines sit, and a line in the wrong place fails
    nothing: the build is correct either way, only minutes slower. So the
    runtime stage is read in order. The first sync may see the lock and
    `pyproject.toml` and nothing else; the second only what hatchling builds
    the wheel from; the console lands after both. Both run as `app` with a
    cache mount, because the alternative -- a `chown -R` once the venv is a
    layer down -- would copy the venv up into the next layer. And the browser
    image, whose uv runs as root, keeps its cache under a different `id`.
    """

    instructions = _dockerfile_instructions(ROOT / "Dockerfile")
    runtime = instructions[
        max(i for i, line in enumerate(instructions) if line.startswith("FROM ")) + 1 :
    ]

    syncs = [
        i
        for i, line in enumerate(runtime)
        if line.startswith("RUN ") and "uv sync" in line
    ]
    assert len(syncs) == 2, [runtime[i] for i in syncs]
    dependencies, project = syncs
    for i in syncs:
        for flag in (
            "--mount=type=cache",
            "--frozen",
            "--no-editable",
            "--extra embedding",
        ):
            assert flag in runtime[i], (flag, runtime[i])
    assert "--no-install-project" in runtime[dependencies]
    assert "--no-install-project" not in runtime[project]

    before = {s for line in runtime[:dependencies] for s in _context_sources(line)}
    assert before == {"pyproject.toml", "uv.lock"}, before
    between = {
        s for line in runtime[dependencies:project] for s in _context_sources(line)
    }
    assert between == {"README.md", "config", "src"}, between
    after = {s for line in runtime[project:] for s in _context_sources(line)}
    assert {"alembic.ini", "migrations", "docker"} <= after, after
    console = next(i for i, line in enumerate(runtime) if "/build/web/dist" in line)
    assert console > project

    users = [line for line in runtime[:dependencies] if line.startswith("USER ")]
    assert users and users[-1] == "USER app:app", users
    assert not any(
        "chown" in line for line in runtime[dependencies:] if line.startswith("RUN ")
    )

    def cache_id(instruction: str) -> str:
        match = re.search(r",id=([^,\s]+)", instruction)
        assert match, f"a cache mount without an explicit id: {instruction}"
        return match.group(1)

    browser = [
        line
        for line in _dockerfile_instructions(ROOT / "docker" / "browser.Dockerfile")
        if line.startswith("RUN ") and "uv sync" in line
    ]
    assert len(browser) == 1, browser
    assert cache_id(browser[0]) != cache_id(runtime[dependencies])
    assert cache_id(runtime[dependencies]) == cache_id(runtime[project])
