"""Building the model adapter a process will actually call.

This is where configuration stops being a promise. Everything up to here has
been validated for shape -- a model id is a non-empty string, a base URL is a
URL -- and none of that says the process can reach a provider. The checks below
are the ones that decide whether starting is honest.

A process that starts without a usable model is worse than one that refuses to.
It passes a health check, accepts requests, and fails every single one at the
provider, which turns a configuration mistake into an incident with a much
longer path back to its cause. So a missing key and an unpinned model id are
both refusals here, in every environment -- the settings layer only rejects
placeholders in production, and a development process that answers nothing is
still a process that answers nothing.
"""

from __future__ import annotations

import httpx

from agent_workbench.adapters.models.deepseek import DeepSeekModel, DeepSeekProfile
from agent_workbench.bootstrap.projections import ModelConfig
from agent_workbench.ports.model import ModelPort

# Shipped in the committed defaults so a fresh checkout is obviously
# unconfigured rather than subtly wrong. Anything carrying this is not a model
# id somebody chose.
PLACEHOLDER_MARKER = "not-configured"


class ModelNotConfiguredError(RuntimeError):
    """The process has no model it could actually call."""


def build_model(config: ModelConfig, *, client: httpx.AsyncClient) -> ModelPort:
    """Assemble the provider adapter, or refuse to start.

    The HTTP client is passed in rather than created here: connection lifetime
    belongs to whoever owns the process, and a client this function opened
    would be one nothing closes.
    """

    if config.provider != "deepseek":
        raise ModelNotConfiguredError(
            f"no adapter is assembled for the {config.provider!r} provider"
        )

    if config.api_key is None or not config.api_key.get_secret_value().strip():
        raise ModelNotConfiguredError(
            "secrets.deepseek_api_key is not configured, so no model call "
            "could succeed; supply it through the environment or .env"
        )

    unpinned = sorted(
        name
        for name, profile in config.profiles.items()
        if PLACEHOLDER_MARKER in profile.model_id
    )
    if unpinned:
        raise ModelNotConfiguredError(
            f"model profiles still carry the placeholder model id: "
            f"{', '.join(unpinned)}; pin the provider's exact model ids"
        )

    return DeepSeekModel(
        client=client,
        api_key=config.api_key.get_secret_value(),
        base_url=config.base_url,
        profiles={
            name: DeepSeekProfile(  # pyright: ignore[reportArgumentType]
                model_id=profile.model_id,
                temperature=profile.temperature,
                max_output_tokens=profile.max_output_tokens,
                timeout_seconds=profile.timeout_seconds,
                max_retries=profile.max_retries,
                tool_calling_required=profile.tool_calling_required,
                thinking=profile.thinking,
                reasoning_effort=profile.reasoning_effort,
            )
            for name, profile in config.profiles.items()
        },
    )


__all__ = ["PLACEHOLDER_MARKER", "ModelNotConfiguredError", "build_model"]
