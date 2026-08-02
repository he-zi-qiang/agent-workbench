"""What a run reports about itself, without naming who collects it.

The architecture guard forbids ``opentelemetry`` in framework-neutral code, and
that is the right rule for the same reason it applies to Qdrant and SQLAlchemy:
a Runtime that imported a telemetry SDK would be a Runtime that could not be
tested without one, and an exporter's failure would become a run's failure.

So the core records against this boundary and an adapter decides what that
means. The default decides it means nothing.

Two shapes, because the questions differ. A **span** answers "what happened
during this, and how long did it take" -- it wraps work and has a beginning and
an end. A **counter** answers "how many times" and a **histogram** answers "how
long, across everything", and neither wraps anything.

Nothing here should carry free text from a model or a document -- the
observability settings pin ``record_prompt_body`` and ``record_tool_result_body``
to False.

``AttributeValue`` is narrow to *say* that, and a sabotage round established
that saying it is all it does: widening the alias to ``object`` produces no type
error, because every call site passes literals that satisfy both. So the alias
is a signal to whoever writes the next call, not a guarantee. What actually
holds the line is a test that reads the values recorded during a real run and
asserts the prompt and the answer are not among them.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import Protocol, runtime_checkable

#: What may describe a measurement. Deliberately not ``Any``: an attribute
#: reaches an exporter, a log line and somebody's bill, and the one thing that
#: must never arrive here is a body somebody's document or model produced.
AttributeValue = str | bool | int | float

Attributes = dict[str, AttributeValue]


@runtime_checkable
class Telemetry(Protocol):
    """Record what a run did. Never raises, whatever the backend is doing."""

    def span(
        self, name: str, *, attributes: Attributes | None = None
    ) -> AbstractContextManager[None]:
        """Wrap a unit of work.

        A context manager rather than start/end calls: the end has to happen on
        the failure path too, and the version of this that takes two calls is
        the version where an early return loses the span.
        """
        ...

    def count(
        self, name: str, *, value: int = 1, attributes: Attributes | None = None
    ) -> None:
        """Record that something happened."""
        ...

    def record(
        self, name: str, value: float, *, attributes: Attributes | None = None
    ) -> None:
        """Record a measurement into a distribution."""
        ...


class NullTelemetry:
    """Records nothing, and is the default everywhere.

    A deployment without observability configured is not a deployment that
    should behave differently, so this is not a degraded mode -- it is the
    absence of a collector, and every call is a no-op that cannot fail.
    """

    __slots__ = ()

    def span(
        self, name: str, *, attributes: Attributes | None = None
    ) -> AbstractContextManager[None]:
        del name, attributes
        return nullcontext()

    def count(
        self, name: str, *, value: int = 1, attributes: Attributes | None = None
    ) -> None:
        del name, value, attributes

    def record(
        self, name: str, value: float, *, attributes: Attributes | None = None
    ) -> None:
        del name, value, attributes


# --------------------------------------------------------------------------
# The names, in one place
# --------------------------------------------------------------------------
#
# Written here rather than at each call site so a dashboard and a test refer to
# the same string, and so renaming one is a change somebody has to make on
# purpose. The set mirrors the metrics the baseline names in §12.3; what is
# absent from this list is absent from the system, which is the honest way for
# it to be discoverable.

RUN_STARTED = "agent.run.started"
RUN_COMPLETED = "agent.run.completed"
RUN_FAILED = "agent.run.failed"
RUN_DURATION = "agent.run.duration_ms"
RUN_STEPS = "agent.run.steps"

TOOL_CALLED = "agent.tool.called"
TOOL_FAILED = "agent.tool.failed"
TOOL_DENIED = "agent.tool.denied"
TOOL_DURATION = "agent.tool.duration_ms"

MODEL_CALLED = "agent.model.called"
MODEL_INPUT_TOKENS = "agent.model.input_tokens"
MODEL_OUTPUT_TOKENS = "agent.model.output_tokens"

RETRIEVAL_DURATION = "rag.retrieval.duration_ms"
RETRIEVAL_CANDIDATES = "rag.retrieval.candidates"
RETRIEVAL_AUTHORIZED = "rag.retrieval.authorized"

TASK_CLAIMED = "task.claimed"
TASK_SETTLED = "task.settled"
TASK_RESUMED = "task.resumed"


__all__ = [
    "MODEL_CALLED",
    "MODEL_INPUT_TOKENS",
    "MODEL_OUTPUT_TOKENS",
    "RETRIEVAL_AUTHORIZED",
    "RETRIEVAL_CANDIDATES",
    "RETRIEVAL_DURATION",
    "RUN_COMPLETED",
    "RUN_DURATION",
    "RUN_FAILED",
    "RUN_STARTED",
    "RUN_STEPS",
    "TASK_CLAIMED",
    "TASK_RESUMED",
    "TASK_SETTLED",
    "TOOL_CALLED",
    "TOOL_DENIED",
    "TOOL_DURATION",
    "TOOL_FAILED",
    "AttributeValue",
    "Attributes",
    "NullTelemetry",
    "Telemetry",
]
