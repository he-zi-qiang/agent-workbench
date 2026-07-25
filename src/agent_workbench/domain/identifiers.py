"""Identifier rules for the trace hierarchy and for provider-supplied ids.

Two kinds of identifier meet inside a run.

Ids the platform mints are prefixed and uniform, so a log line, an event and a
database row all say what they point at. Ids that arrive from a model provider
-- tool call ids above all -- are opaque: they must survive every conversion
byte for byte, otherwise a ToolResult can no longer be paired with its
ToolCall. The constraint below is therefore permissive about shape and strict
about safety: printable, bounded, and free of characters that would break a
log line, a URL or an SSE frame.

A provider tool call id is never an idempotency key on its own. A retried model
turn mints a new one for the same intent, so external side effects key off a
stable business ``operation_key`` instead.
"""

from __future__ import annotations

from typing import Annotated, Final
from uuid import uuid4

from pydantic import StringConstraints

ID_MAX_LENGTH: Final[int] = 128
ID_PATTERN: Final[str] = r"^[A-Za-z0-9][A-Za-z0-9_.:+=@-]{0,127}$"

Identifier = Annotated[
    str,
    StringConstraints(min_length=1, max_length=ID_MAX_LENGTH, pattern=ID_PATTERN),
]

AGENT_RUN_ID_PREFIX: Final[str] = "run"
APPROVAL_ID_PREFIX: Final[str] = "apr"
ARTIFACT_ID_PREFIX: Final[str] = "art"
EVENT_ID_PREFIX: Final[str] = "evt"
GRAPH_NODE_ID_PREFIX: Final[str] = "node"
MESSAGE_ID_PREFIX: Final[str] = "msg"
MODEL_CALL_ID_PREFIX: Final[str] = "mc"
STREAM_ID_PREFIX: Final[str] = "stream"
TASK_ID_PREFIX: Final[str] = "task"
TOOL_CALL_ID_PREFIX: Final[str] = "tc"
WORKFLOW_THREAD_ID_PREFIX: Final[str] = "thread"


def new_id(prefix: str) -> str:
    """Mint a prefixed, collision-free identifier."""

    return f"{prefix}_{uuid4().hex}"


def new_agent_run_id() -> str:
    return new_id(AGENT_RUN_ID_PREFIX)


def new_approval_id() -> str:
    return new_id(APPROVAL_ID_PREFIX)


def new_artifact_id() -> str:
    return new_id(ARTIFACT_ID_PREFIX)


def new_event_id() -> str:
    return new_id(EVENT_ID_PREFIX)


def new_graph_node_id() -> str:
    return new_id(GRAPH_NODE_ID_PREFIX)


def new_message_id() -> str:
    return new_id(MESSAGE_ID_PREFIX)


def new_model_call_id() -> str:
    return new_id(MODEL_CALL_ID_PREFIX)


def new_stream_id() -> str:
    return new_id(STREAM_ID_PREFIX)


def new_task_id() -> str:
    return new_id(TASK_ID_PREFIX)


def new_tool_call_id() -> str:
    """Mint a tool call id.

    Production tool call ids come from the model provider and are preserved as
    received. This helper exists for the deterministic FakeModel and for tests,
    which need the same pairing guarantees without a provider.
    """

    return new_id(TOOL_CALL_ID_PREFIX)


def new_workflow_thread_id() -> str:
    return new_id(WORKFLOW_THREAD_ID_PREFIX)


__all__ = [
    "AGENT_RUN_ID_PREFIX",
    "APPROVAL_ID_PREFIX",
    "ARTIFACT_ID_PREFIX",
    "EVENT_ID_PREFIX",
    "GRAPH_NODE_ID_PREFIX",
    "ID_MAX_LENGTH",
    "ID_PATTERN",
    "MESSAGE_ID_PREFIX",
    "MODEL_CALL_ID_PREFIX",
    "STREAM_ID_PREFIX",
    "TASK_ID_PREFIX",
    "TOOL_CALL_ID_PREFIX",
    "WORKFLOW_THREAD_ID_PREFIX",
    "Identifier",
    "new_agent_run_id",
    "new_approval_id",
    "new_artifact_id",
    "new_event_id",
    "new_graph_node_id",
    "new_id",
    "new_message_id",
    "new_model_call_id",
    "new_stream_id",
    "new_task_id",
    "new_tool_call_id",
    "new_workflow_thread_id",
]
