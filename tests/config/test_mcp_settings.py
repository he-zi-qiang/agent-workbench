"""The `[mcp]` section (ADR-025, PR-1).

Every test here pairs a rejection with the control that must still be accepted.
A test that only asserts "this is refused" cannot tell a working validator from
one that refuses everything.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_workbench.bootstrap.projections import WORKSPACE_TOOLS, project_task
from agent_workbench.bootstrap.settings import Settings
from tests.config.test_settings import valid_payload

SERVER = {
    "alias": "office",
    "transport": "http",
    "endpoint": "http://127.0.0.1:9100",
    "tools": ["render_document"],
    "retryable_effects": False,
    "timeout_seconds": 30,
}


def payload_with_servers(*servers: dict) -> dict:
    payload = valid_payload()
    payload["optional_labs"]["mcp_adapter"] = True
    payload["mcp"] = {"servers": list(servers)}
    return payload


def test_a_deployment_that_never_configured_mcp_gets_no_servers() -> None:
    # The default shape matters on its own: it is what keeps an upgrade from
    # widening anything for a deployment that never asked for this feature.
    settings = Settings(**valid_payload())

    assert settings.mcp.servers == ()
    assert settings.optional_labs.mcp_adapter is False


def test_the_mcp_section_ships_under_the_current_schema_version() -> None:
    """Named for the section rather than for a number, after 1.10 -> 1.11.

    The version moved for a reason unrelated to MCP, and a test called
    ``..._moved_to_1_10`` then asserted something that was no longer true and
    no longer about MCP. The reasoning behind each bump lives in one place:
    ``test_the_configuration_schema_version_is_pinned``.
    """

    settings = Settings(**valid_payload())

    assert settings.app.config_schema_version == "1.16"


def test_retryable_server_tools_are_frozen_into_new_task_authority() -> None:
    settings = Settings(
        **payload_with_servers(
            {
                **SERVER,
                "tools": ["render-document", "lookup"],
                "retryable_effects": True,
            }
        )
    )

    envelope = project_task(settings).default_authorization_envelope

    assert envelope.allowed_tools == (
        "export_artifact",
        "mcp_office_lookup",
        "mcp_office_render_document",
        *WORKSPACE_TOOLS,
    )
    assert envelope.max_tool_risk == "external"


def test_nonretryable_server_tools_never_enter_task_authority() -> None:
    nonretryable = Settings(**payload_with_servers(SERVER))
    retryable = Settings(**payload_with_servers({**SERVER, "retryable_effects": True}))

    # The workspace tools ride in both: they are not outward, so a server the
    # deployment refused to trust does not gain or lose anything by their
    # presence. What this pins is that the *MCP* name is the only difference.
    assert project_task(nonretryable).default_authorization_envelope.allowed_tools == (
        "export_artifact",
        *WORKSPACE_TOOLS,
    )
    assert project_task(retryable).default_authorization_envelope.allowed_tools == (
        "export_artifact",
        "mcp_office_render_document",
        *WORKSPACE_TOOLS,
    )


def test_each_server_requires_an_explicit_nonempty_tool_allowlist() -> None:
    without = {key: value for key, value in SERVER.items() if key != "tools"}

    for rejected in (without, {**SERVER, "tools": []}):
        with pytest.raises(ValidationError) as excinfo:
            Settings(**payload_with_servers(rejected))
        assert "tools" in str(excinfo.value)

    settings = Settings(**payload_with_servers(SERVER))
    assert settings.mcp.servers[0].tools == ("render_document",)


def test_an_allowlist_cannot_hide_a_normalized_name_collision() -> None:
    with pytest.raises(ValidationError, match="normalize to the same"):
        Settings(
            **payload_with_servers(
                {**SERVER, "tools": ["render-document", "render.document"]}
            )
        )

    settings = Settings(
        **payload_with_servers({**SERVER, "tools": ["render", "lookup"]})
    )
    assert settings.mcp.servers[0].tools == ("render", "lookup")


def test_cross_server_namespace_boundaries_cannot_hide_a_collision() -> None:
    settings = Settings(
        **payload_with_servers(
            {
                **SERVER,
                "alias": "office_suite",
                "tools": ["render"],
                "retryable_effects": True,
            },
            {
                **SERVER,
                "alias": "office",
                "endpoint": "http://127.0.0.1:9101",
                "tools": ["suite_render"],
                "retryable_effects": True,
            },
        )
    )

    with pytest.raises(ValueError, match="collide after local name normalization"):
        project_task(settings)


def test_retryable_effects_has_no_default_and_must_be_stated() -> None:
    without = {
        key: value for key, value in SERVER.items() if key != "retryable_effects"
    }

    with pytest.raises(ValidationError) as excinfo:
        Settings(**payload_with_servers(without))
    assert "retryable_effects" in str(excinfo.value)

    # Control: the same server, with the field stated either way, is accepted.
    for stated in (True, False):
        settings = Settings(
            **payload_with_servers({**SERVER, "retryable_effects": stated})
        )
        assert settings.mcp.servers[0].retryable_effects is stated


def test_two_servers_may_not_share_an_alias() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            **payload_with_servers(
                SERVER, {**SERVER, "endpoint": "http://127.0.0.1:9101"}
            )
        )
    assert "alias" in str(excinfo.value)

    # Control: the same two servers under distinct aliases are accepted, and
    # order is preserved -- the alias is a naming-space segment, so a registry
    # built from this must be reproducible.
    settings = Settings(**payload_with_servers(SERVER, {**SERVER, "alias": "sheets"}))
    assert [server.alias for server in settings.mcp.servers] == ["office", "sheets"]


def test_an_alias_must_fit_the_tool_name_segment() -> None:
    # It becomes part of a ToolName, which is `^[a-z][a-z0-9_]{0,63}$`.
    for rejected in ("Office", "my-server", "9lives", ""):
        with pytest.raises(ValidationError):
            Settings(**payload_with_servers({**SERVER, "alias": rejected}))

    settings = Settings(**payload_with_servers({**SERVER, "alias": "office_v2"}))
    assert settings.mcp.servers[0].alias == "office_v2"


def test_stdio_transport_is_refused_rather_than_quietly_accepted() -> None:
    # Spawning a local subprocess is a different threat model than calling a
    # service, and ADR-025 did not decide it. Refusing the value keeps that an
    # explicit future change instead of something a config file can turn on.
    with pytest.raises(ValidationError):
        Settings(**payload_with_servers({**SERVER, "transport": "stdio"}))

    settings = Settings(**payload_with_servers({**SERVER, "transport": "http"}))
    assert settings.mcp.servers[0].transport == "http"


def test_configuring_servers_with_the_lab_switch_off_is_a_startup_error() -> None:
    payload = payload_with_servers(SERVER)
    payload["optional_labs"]["mcp_adapter"] = False

    with pytest.raises(ValidationError) as excinfo:
        Settings(**payload)
    assert "mcp_adapter" in str(excinfo.value)

    # Control: the switch on, same servers -- accepted. And the switch on with
    # no servers is also fine; that is a deployment that turned the lab on and
    # has not pointed it anywhere yet.
    assert Settings(**payload_with_servers(SERVER)).mcp.servers
    empty = valid_payload()
    empty["optional_labs"]["mcp_adapter"] = True
    assert Settings(**empty).mcp.servers == ()


def test_an_endpoint_may_not_carry_credentials() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            **payload_with_servers(
                {**SERVER, "endpoint": "http://user:secret@127.0.0.1:9100"}
            )
        )
    assert "endpoint" in str(excinfo.value).lower()

    settings = Settings(
        **payload_with_servers({**SERVER, "endpoint": "https://mcp.example.test"})
    )
    assert settings.mcp.servers[0].endpoint == "https://mcp.example.test"


def test_plain_http_is_limited_to_loopback_endpoints() -> None:
    for endpoint in (
        "http://mcp.example.test/mcp",
        "http://192.0.2.10:9100/mcp",
    ):
        with pytest.raises(ValidationError, match="HTTPS"):
            Settings(**payload_with_servers({**SERVER, "endpoint": endpoint}))

    for endpoint in (
        "http://localhost:9100/mcp",
        "http://127.0.0.1:9100/mcp",
        "http://[::1]:9100/mcp",
        "https://mcp.example.test/mcp",
    ):
        settings = Settings(**payload_with_servers({**SERVER, "endpoint": endpoint}))
        assert settings.mcp.servers[0].endpoint == endpoint


def test_a_server_that_states_no_audience_keeps_the_one_it_has_today() -> None:
    """The anti-regression default (ADR-027 §3.3).

    Every configuration written before this field existed meant `synthesis`:
    ADR-025 gave the dynamic catalog to `writer/synthesize` and to nothing
    else. A default of `research` would silently move the Word renderer off the
    writer on upgrade.
    """

    settings = Settings(**payload_with_servers(SERVER))

    assert settings.mcp.servers[0].audience == "synthesis"


def test_both_audiences_are_accepted_and_nothing_else_is() -> None:
    for audience in ("research", "synthesis"):
        settings = Settings(**payload_with_servers({**SERVER, "audience": audience}))
        assert settings.mcp.servers[0].audience == audience

    # A free-form value would make "which agent may reach this tool" a question
    # about whichever config file was written last.
    with pytest.raises(ValidationError):
        Settings(**payload_with_servers({**SERVER, "audience": "everyone"}))


def test_the_audience_does_not_change_what_the_task_envelope_allows() -> None:
    """The envelope is the Task\'s ceiling; the audience is which agent may
    reach up to it. Conflating them would make a research tool unusable by a
    Task submitted while it was declared for synthesis."""

    research = Settings(
        **payload_with_servers(
            {**SERVER, "retryable_effects": True, "audience": "research"}
        )
    )
    synthesis = Settings(
        **payload_with_servers(
            {**SERVER, "retryable_effects": True, "audience": "synthesis"}
        )
    )

    assert (
        project_task(research).default_authorization_envelope
        == project_task(synthesis).default_authorization_envelope
    )
