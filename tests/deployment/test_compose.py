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
