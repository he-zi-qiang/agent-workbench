"""External-research adapters."""

from agent_workbench.adapters.research.anthropic_web_search import (
    AnthropicWebSearch,
    AnthropicWebSearchUnavailableError,
    build_anthropic_web_search,
)

__all__ = [
    "AnthropicWebSearch",
    "AnthropicWebSearchUnavailableError",
    "build_anthropic_web_search",
]
