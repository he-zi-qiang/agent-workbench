"""Agent executor implementations.

Exactly one component may own a model-tool loop. The custom runtime will own it
from WP02 onward; what lives here until then is a single-turn walking skeleton
that owns no loop at all and says so when asked to run one.
"""

from agent_workbench.adapters.agents.single_turn import (
    DEFAULT_MODEL_LABEL,
    SingleTurnAgentExecutor,
)

__all__ = ["DEFAULT_MODEL_LABEL", "SingleTurnAgentExecutor"]
