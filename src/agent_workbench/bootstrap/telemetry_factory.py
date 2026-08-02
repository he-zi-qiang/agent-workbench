"""Turning the observability settings into something that actually records.

``observability.otel_enabled`` has been ``Literal[True]`` since the settings
were written, and until this module existed there was nothing behind it: a flag
that could not be turned off and did not turn anything on. This is the factory
that makes the claim true, and the reason the flag stays pinned is now the
honest one -- a deployment cannot opt out of being observable, only out of
having somewhere to send it.

Assembly here follows the same rule as every other factory in this package: the
process decides once, at startup, and hands finished objects down. What it does
*not* do is fail closed. Every other factory refuses to build what it cannot
support, because a process that cannot reach its model or its index cannot do
its job. A process that cannot reach its collector can: it just does it
unobserved, and trading "the service runs" for "the service is measured" is a
bad trade nobody asked for.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_workbench.adapters.telemetry import build_otel_telemetry, shutdown
from agent_workbench.bootstrap.projections import ObservabilityConfig
from agent_workbench.ports.telemetry import NullTelemetry, Telemetry


@dataclass(frozen=True, slots=True)
class AssembledTelemetry:
    """What a process records with, and what it has to stop at exit.

    ``dispose`` is here rather than left to the caller because a batch span
    processor holds unflushed spans: dropping them at exit loses exactly the
    tail of a run, which is the part somebody is usually looking for.
    """

    telemetry: Telemetry
    _dispose: object = None

    async def dispose(self) -> None:
        if self._dispose is not None:
            self._dispose()  # type: ignore[operator]


def build_telemetry(config: ObservabilityConfig | None) -> AssembledTelemetry:
    """Build a collector, or the absence of one.

    Absent configuration and a failed exporter both produce ``NullTelemetry``.
    They are the same answer on purpose: in both cases nothing is collected,
    and a process that behaved differently between them would make "is this
    deployment observable" a question about which failure happened.

    Which means the early return below is redundant with the ``except``, and a
    sabotage round confirmed it: removing it changes nothing observable,
    because ``None.service_name`` raises and lands in the same fallback. It
    stays as the statement of intent -- "no configuration" is a normal state,
    not an error being swallowed -- and it becomes load-bearing the day the
    fallback narrows to specific exception types.
    """

    if config is None:
        return AssembledTelemetry(telemetry=NullTelemetry())

    try:
        telemetry, tracer_provider, meter_provider = build_otel_telemetry(
            service_name=config.service_name,
            endpoint=config.exporter_endpoint,
            sample_ratio=config.trace_sample_ratio,
            metrics_enabled=config.metrics_enabled,
        )
    except Exception:
        # Constructing an exporter can fail on a malformed endpoint, and that
        # is a configuration mistake worth surviving rather than crashing on:
        # the alternative is a deployment that will not start because its
        # telemetry backend is misspelled.
        return AssembledTelemetry(telemetry=NullTelemetry())

    return AssembledTelemetry(
        telemetry=telemetry,
        _dispose=lambda: shutdown(tracer_provider, meter_provider),
    )


__all__ = ["AssembledTelemetry", "build_telemetry"]
