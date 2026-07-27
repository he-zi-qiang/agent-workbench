"""What has to be true before a process claims it can answer.

Every refusal here describes a process that would otherwise start, pass a
health check, accept requests, and fail all of them at the provider. That turns
a configuration mistake into an incident whose cause is several layers away
from its symptom, which is the whole reason these are startup errors.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from agent_workbench.adapters.models.deepseek import DeepSeekModel
from agent_workbench.bootstrap.model_factory import (
    ModelNotConfiguredError,
    build_model,
)
from agent_workbench.bootstrap.projections import ModelConfig, ModelProfileConfig
from agent_workbench.ports.model import ModelPort

PINNED = "deepseek-chat"


def _profile(model_id: str = PINNED) -> ModelProfileConfig:
    return ModelProfileConfig(
        model_id=model_id,
        temperature=0.0,
        max_output_tokens=1024,
        timeout_seconds=30.0,
        max_retries=1,
        tool_calling_required=True,
    )


def _config(**overrides: object) -> ModelConfig:
    fields: dict[str, object] = {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "api_key": SecretStr("sk-configured"),
        "profiles": {"main": _profile(), "compact": _profile()},
    }
    fields.update(overrides)
    return ModelConfig(**fields)  # pyright: ignore[reportArgumentType]


def _build(config: ModelConfig) -> ModelPort:
    # Never sent through, so it is never opened: every assertion here is about
    # what happens before the first request.
    return build_model(config, client=httpx.AsyncClient())


def test_a_configured_process_gets_a_model() -> None:
    """The control: these refusals are about configuration, not about building."""

    model = _build(_config())

    assert isinstance(model, DeepSeekModel)
    assert isinstance(model, ModelPort)


def test_a_missing_api_key_refuses_to_assemble() -> None:
    """Starting without one means answering nothing, one request at a time."""

    with pytest.raises(ModelNotConfiguredError, match="deepseek_api_key"):
        _build(_config(api_key=None))


def test_a_blank_api_key_refuses_too() -> None:
    """An empty string is how a missing environment variable usually arrives."""

    with pytest.raises(ModelNotConfiguredError, match="deepseek_api_key"):
        _build(_config(api_key=SecretStr("   ")))


def test_a_placeholder_model_id_refuses_to_assemble() -> None:
    """Settings only rejects placeholders in production.

    A development process that answers nothing is still a process that answers
    nothing, so assembly refuses one in every environment.
    """

    with pytest.raises(ModelNotConfiguredError, match="placeholder"):
        _build(
            _config(
                profiles={
                    "main": _profile("not-configured-deepseek-main"),
                    "compact": _profile(),
                }
            )
        )


def test_the_refusal_names_which_profiles_are_unpinned() -> None:
    """A message that says "something is wrong" costs a bisect to act on."""

    with pytest.raises(ModelNotConfiguredError, match="compact, main"):
        _build(
            _config(
                profiles={
                    "main": _profile("not-configured-deepseek-main"),
                    "compact": _profile("not-configured-deepseek-compact"),
                }
            )
        )


def test_a_provider_with_no_adapter_refuses() -> None:
    """Configuration can name a provider this build cannot speak to."""

    with pytest.raises(ModelNotConfiguredError, match="openai"):
        _build(_config(provider="openai"))


def test_the_key_does_not_appear_in_the_refusal() -> None:
    """Startup errors reach logs and issue trackers."""

    try:
        _build(_config(provider="openai", api_key=SecretStr("sk-canary")))
    except ModelNotConfiguredError as refusal:
        assert "sk-canary" not in str(refusal)
