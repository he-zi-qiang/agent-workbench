"""P0-1. The identity boundary, checked against a socket rather than a string.

ADR-012 rested on a premise: the header identity resolver only ever runs behind
a process bound to loopback. Nothing enforced it, the committed default was
``0.0.0.0``, and the test that looked like it guarded the boundary guarded a
different object -- it asserted that ``remote`` scope refuses to assemble, which
is true and does not stop a ``local`` process from listening on every interface.

So these bind. The first half asserts the rule at the two layers that can refuse
before a socket exists; the second half opens a real one and tries to reach it
from this machine's own routable address. The reachability pair carries its own
control: the same connection must succeed against a wildcard bind. Without it,
"connection refused" could mean the guard works, or it could mean the test was
never pointed at anything.
"""

from __future__ import annotations

import dataclasses
import socket
import tomllib
from contextlib import closing
from typing import Any

import pytest
from pydantic import ValidationError

from agent_workbench.apps.api.dependencies import (
    InsecureDeploymentError,
    build_dependencies,
)
from agent_workbench.bootstrap.network import is_loopback_bind_address
from agent_workbench.bootstrap.paths import DEFAULT_CONFIG_FILE
from agent_workbench.bootstrap.projections import ApiRuntimeConfig, project_api
from agent_workbench.bootstrap.settings import Settings

# A syntactically valid DSN is enough: settings validation never connects.
UNUSED_DSN = "postgresql+asyncpg://unit:unit@127.0.0.1:5432/unit"

# TEST-NET-1 (RFC 5737). Connecting a UDP socket to it sends nothing; it only
# makes the kernel choose the source address it would route from.
UNROUTED_PROBE = "192.0.2.1"


def _committed_defaults() -> dict[str, Any]:
    with DEFAULT_CONFIG_FILE.open("rb") as handle:
        payload: dict[str, Any] = tomllib.load(handle)
    payload["database"] = dict(payload.get("database", {}))
    payload["database"].update(
        dsn=UNUSED_DSN, guard_dsn=UNUSED_DSN, listen_dsn=UNUSED_DSN
    )
    payload["model"]["main"]["model_id"] = "unit-main"
    payload["model"]["compact"]["model_id"] = "unit-compact"
    payload["secrets"] = {"deepseek_api_key": "unit-test-key"}
    return payload


def _settings(**api_overrides: Any) -> Settings:
    payload = _committed_defaults()
    payload["api"] = {**payload["api"], **api_overrides}
    return Settings(**payload)


def _routable_address() -> str | None:
    """This machine's own off-box address, or None if it has none."""

    with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as probe:
        try:
            probe.connect((UNROUTED_PROBE, 9))
            address: str = probe.getsockname()[0]
        except OSError:
            return None
    return None if is_loopback_bind_address(address) else address


def _bind(host: str) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind((host, 0))
    listener.listen(1)
    return listener


def _reachable_at(address: str, port: int) -> bool:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as caller:
        caller.settimeout(2.0)
        return caller.connect_ex((address, port)) == 0


# --- the rule, before a socket exists ----------------------------------------


def test_the_committed_default_host_is_loopback() -> None:
    """The value a fresh checkout serves on, read from the file itself."""

    payload = _committed_defaults()

    assert is_loopback_bind_address(payload["api"]["host"])


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "10.0.0.4", "192.168.1.20", "0.0.0.0:8000"],
)
def test_settings_refuse_a_host_reachable_from_other_machines(host: str) -> None:
    with pytest.raises(ValidationError, match="loopback"):
        _settings(host=host)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_settings_accept_the_loopback_forms(host: str) -> None:
    assert _settings(host=host).api.host == host


def test_a_name_that_is_not_localhost_is_refused_rather_than_resolved() -> None:
    """Resolution can mean one thing at validation and another at bind."""

    with pytest.raises(ValidationError, match="loopback"):
        _settings(host="api.internal")


def test_assembly_refuses_a_reachable_host_that_arrived_another_way() -> None:
    """Settings is not the only way to hold an ApiRuntimeConfig."""

    config = dataclasses.replace(project_api(_settings()), host="0.0.0.0")

    with pytest.raises(InsecureDeploymentError, match="loopback"):
        build_dependencies(config)


def test_assembly_accepts_the_configured_default() -> None:
    """The refusal is specific; it does not simply reject every deployment."""

    config: ApiRuntimeConfig = project_api(_settings())
    dependencies = build_dependencies(config)

    assert is_loopback_bind_address(dependencies.config.host)


# --- the rule, against a socket that exists ----------------------------------


def test_the_default_host_binds_a_socket_that_is_loopback() -> None:
    """Not "the string looks right" -- the address the kernel actually assigned.

    Bound from the raw file rather than through ``Settings``, deliberately. A
    socket test that only ever sees validator-approved values cannot catch the
    validators being wrong; this one is meant to hold when they are gone.
    """

    with closing(_bind(_committed_defaults()["api"]["host"])) as listener:
        assert is_loopback_bind_address(listener.getsockname()[0])


def test_the_default_host_is_not_reachable_at_this_machines_own_address() -> None:
    """The property itself: a real connection to a real listener, refused."""

    routable = _routable_address()
    if routable is None:
        pytest.skip("this host has no non-loopback IPv4 address to be reached at")

    with closing(_bind(_committed_defaults()["api"]["host"])) as listener:
        port = listener.getsockname()[1]

        assert not _reachable_at(routable, port)


def test_a_wildcard_bind_is_reachable_there(  # the control for the test above
) -> None:
    """Proves the refusal above is the bind address, not a broken probe.

    If this fails, the unreachability test proves nothing: it would report a
    clean result against a port nothing could ever have answered on.
    """

    routable = _routable_address()
    if routable is None:
        pytest.skip("this host has no non-loopback IPv4 address to be reached at")

    with closing(_bind("0.0.0.0")) as listener:
        port = listener.getsockname()[1]

        assert _reachable_at(routable, port)
