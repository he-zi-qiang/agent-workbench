"""External-research adapters."""

from agent_workbench.adapters.research.deepseek_web_search import (
    DeepSeekWebSearch,
    WebSearchUnavailableError,
)

__all__ = [
    "DeepSeekWebSearch",
    "WebSearchUnavailableError",
]
