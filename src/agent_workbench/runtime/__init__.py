"""The custom agent runtime.

One component owns the model-tool loop, and this is it. The package depends on
the domain and the ports and on nothing else: swapping a model provider, a
vector store or a workflow engine must leave the loop untouched, which is the
property the architecture tests enforce.
"""

from agent_workbench.runtime.agent_runtime import (
    DEFAULT_MODEL_LABEL,
    ClaudeLikeAgentRuntime,
)
from agent_workbench.runtime.hook_bus import HookBus, HookBusOutcome
from agent_workbench.runtime.schema_validation import (
    SUPPORTED_KEYWORDS,
    UnsupportedToolSchema,
    assert_schema_supported,
    validate_arguments,
)
from agent_workbench.runtime.state import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    InvalidStateTransition,
    RunStateMachine,
)
from agent_workbench.runtime.tool_executor import ToolExecutor
from agent_workbench.runtime.tool_gateway import PreparedCall, ToolGateway

__all__ = [
    "ALLOWED_TRANSITIONS",
    "DEFAULT_MODEL_LABEL",
    "SUPPORTED_KEYWORDS",
    "TERMINAL_STATES",
    "ClaudeLikeAgentRuntime",
    "HookBus",
    "HookBusOutcome",
    "InvalidStateTransition",
    "PreparedCall",
    "RunStateMachine",
    "ToolExecutor",
    "ToolGateway",
    "UnsupportedToolSchema",
    "assert_schema_supported",
    "validate_arguments",
]
