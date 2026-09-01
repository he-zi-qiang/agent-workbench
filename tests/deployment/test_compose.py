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
