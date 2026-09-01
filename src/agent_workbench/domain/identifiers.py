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
ARTIFACT_ID_PREFIX: Final[str] = "art"
EVENT_ID_PREFIX: Final[str] = "evt"
EVIDENCE_ID_PREFIX: Final[str] = "evidence"
MESSAGE_ID_PREFIX: Final[str] = "msg"
MODEL_CALL_ID_PREFIX: Final[str] = "mc"
SESSION_ID_PREFIX: Final[str] = "ses"
TASK_ID_PREFIX: Final[str] = "task"
TOOL_CALL_ID_PREFIX: Final[str] = "tc"
TOOL_EXECUTION_ID_PREFIX: Final[str] = "texec"
CONVERSATION_TURN_ID_PREFIX: Final[str] = "turn"
#: The LangGraph thread a Task's checkpoints hang off.
#:
#: `"thr"`, not `"thread"`, and the difference is not cosmetic: `"thr"` is what
#: is in the database. This constant read `"thread"` and had no callers, while
#: `application/tasks.py` declared a second `TASK_THREAD_PREFIX = "thr"` and
#: minted from that -- two declarations of one concept, in two layers,
#: disagreeing, with the domain's being the dead one. Corrected to what
#: production mints, which changes no stored bytes (2026-08-31).
WORKFLOW_THREAD_ID_PREFIX: Final[str] = "thr"

#: **Two different objects carry an "approval id", and they are not the same
#: thing.** `apr_` is the transient question a tool call hands an interactive
#: gate (`runtime/tool_gateway.py`); `approval_` is the durable Task approval
#: row with its own `decision_version` and ledger
#: (`adapters/persistence/approvals.py`). Both prefixes are real and both stay;
#: declaring them side by side is what makes the distinction deliberate rather
#: than an accident of two call sites.
APPROVAL_ID_PREFIX: Final[str] = "apr"
TASK_APPROVAL_ID_PREFIX: Final[str] = "approval"

# Two prefixes were deleted here on 2026-08-31, and what they claimed is worth
# recording because it was not merely unused.
#
# `GRAPH_NODE_ID_PREFIX = "node"` with a minter: graph nodes are **named** by
# the graph declaration -- `understand`, `plan`, `route` -- and never minted. A
# minter for them describes a model this system does not have.
#
# `STREAM_ID_PREFIX = "stream"` with a minter, and this one was contradicted
# rather than just idle. A stream id is either *borrowed* (chat and code use
# the session id: `stream_id=request.session_id`) or minted by whoever owns
# that stream (`triage`, `kgx`). Declaring a generic `stream_` here said
# streams have an identity space of their own. They do not.


def new_id(prefix: str) -> str:
    """Mint a prefixed, collision-free identifier."""

    return f"{prefix}_{uuid4().hex}"


def new_agent_run_id() -> str:
    return new_id(AGENT_RUN_ID_PREFIX)


def new_approval_id() -> str:
    """Mint the id of a tool call waiting on a person (see the note above)."""

    return new_id(APPROVAL_ID_PREFIX)


def new_task_approval_id() -> str:
    """Mint the id of a durable Task approval row (see the note above)."""

    return new_id(TASK_APPROVAL_ID_PREFIX)


def new_artifact_id() -> str:
    return new_id(ARTIFACT_ID_PREFIX)


def new_event_id() -> str:
    return new_id(EVENT_ID_PREFIX)


def new_evidence_id() -> str:
    return new_id(EVIDENCE_ID_PREFIX)


def new_message_id() -> str:
    return new_id(MESSAGE_ID_PREFIX)


def new_model_call_id() -> str:
    return new_id(MODEL_CALL_ID_PREFIX)


def new_session_id() -> str:
    """Mint a chat or code session id.

    One minter for both, deliberately: the two are the same kind of thing to
    everything downstream -- an event stream is addressed by it, a conversation
    hangs off it -- and they were two `new_id("ses")` literals in two
    application modules before this existed.
    """

    return new_id(SESSION_ID_PREFIX)


def new_tool_execution_id() -> str:
    return new_id(TOOL_EXECUTION_ID_PREFIX)


def new_conversation_turn_id() -> str:
    """Mint a chat turn id.

    Both conversation stores mint these, and before this existed they did it
    with the same string literal in two files -- the in-memory double and the
    PostgreSQL implementation, which the contract suite runs the *same* tests
    against. Those tests do not assert the prefix, so a divergence between the
    two would have been invisible exactly where this repository claims a
    divergence becomes a failure.
    """

    return new_id(CONVERSATION_TURN_ID_PREFIX)


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
    "CONVERSATION_TURN_ID_PREFIX",
    "EVENT_ID_PREFIX",
    "EVIDENCE_ID_PREFIX",
    "ID_MAX_LENGTH",
    "ID_PATTERN",
    "MESSAGE_ID_PREFIX",
    "MODEL_CALL_ID_PREFIX",
    "SESSION_ID_PREFIX",
    "TASK_APPROVAL_ID_PREFIX",
    "TASK_ID_PREFIX",
    "TOOL_CALL_ID_PREFIX",
    "TOOL_EXECUTION_ID_PREFIX",
    "WORKFLOW_THREAD_ID_PREFIX",
    "Identifier",
    "new_agent_run_id",
    "new_approval_id",
    "new_artifact_id",
    "new_conversation_turn_id",
    "new_event_id",
    "new_evidence_id",
    "new_id",
    "new_message_id",
    "new_model_call_id",
    "new_session_id",
    "new_task_approval_id",
    "new_task_id",
    "new_tool_call_id",
    "new_tool_execution_id",
    "new_workflow_thread_id",
]
