"""``Telemetry`` over OpenTelemetry, and the failures it refuses to propagate.

The one rule this adapter exists to keep: **a collector's problem is never a
run's problem**. An exporter that is unreachable, slow, or misconfigured must
not fail a chat turn or a Task, so every method here swallows what the SDK
raises. That is not defensive habit -- an agent run has a budget, a lease and a
human on the other end, and none of them should be spent on a metrics backend.

What is deliberately *not* here is a second place that decides what to record.
The names live in ``ports.telemetry``, and this module maps them onto
instruments the first time each is asked for. A dashboard and a test therefore
refer to the same string, and there is no list here that can drift from that
one.

Bodies are not supposed to arrive, and this module does nothing to stop one:
attributes are passed through to the SDK as given. The narrow ``AttributeValue``
alias is a signal rather than a guard -- see ``ports.telemetry`` -- and the
actual check is a test over what a real run records.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager, suppress
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

from agent_workbench.ports.telemetry import Attributes

#: Metric names ending in one of these are durations. Chosen by suffix rather
#: than by a table, so a name added to ``ports.telemetry`` gets the right
#: instrument without a second list having to learn about it.
_MILLISECOND_SUFFIX = "_ms"


class OtelTelemetry:
    """Record spans and metrics, and never let the collector break a run."""

    __slots__ = ("_counters", "_histograms", "_meter", "_tracer")

    def __init__(self, *, tracer: Any, meter: Any) -> None:
        self._tracer = tracer
        self._meter = meter
        self._counters: dict[str, Any] = {}
        self._histograms: dict[str, Any] = {}

    def span(
        self, name: str, *, attributes: Attributes | None = None
    ) -> AbstractContextManager[None]:
        return self._span(name, attributes)

    @contextmanager
    def _span(self, name: str, attributes: Attributes | None) -> Generator[None]:
        try:
            started = self._tracer.start_as_current_span(
                name, attributes=dict(attributes or {})
            )
        except Exception:
            # Starting failed, so there is nothing to end. The work still runs.
            yield
            return
        with started:
            yield

    def count(
        self, name: str, *, value: int = 1, attributes: Attributes | None = None
    ) -> None:
        with suppress(Exception):
            self._counter(name).add(value, dict(attributes or {}))

    def record(
        self, name: str, value: float, *, attributes: Attributes | None = None
    ) -> None:
        with suppress(Exception):
            self._histogram(name).record(value, dict(attributes or {}))

    def _counter(self, name: str) -> Any:
        instrument = self._counters.get(name)
        if instrument is None:
            instrument = self._meter.create_counter(name)
            self._counters[name] = instrument
        return instrument

    def _histogram(self, name: str) -> Any:
        instrument = self._histograms.get(name)
        if instrument is None:
            unit = "ms" if name.endswith(_MILLISECOND_SUFFIX) else ""
            instrument = self._meter.create_histogram(name, unit=unit)
            self._histograms[name] = instrument
        return instrument


def build_otel_telemetry(
    *,
    service_name: str,
    endpoint: str,
    sample_ratio: float,
    metrics_enabled: bool,
) -> tuple[OtelTelemetry, TracerProvider, MeterProvider | None]:
    """Assemble providers this process owns, and hand back what shuts them down.

    The providers are returned rather than only registered globally because a
    process that starts them has to stop them: a batch span processor holds
    unflushed spans, and dropping them at exit is the one loss that looks like
    "the run never happened".

    Registered globally as well, so a library that reaches for the global
    tracer -- as instrumentation packages do -- finds this one rather than a
    second, unexported provider.
    """

    resource = Resource.create({"service.name": service_name})
    tracer_provider = TracerProvider(
        resource=resource, sampler=TraceIdRatioBased(sample_ratio)
    )
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
    )
    trace.set_tracer_provider(tracer_provider)

    meter_provider: MeterProvider | None = None
    if metrics_enabled:
        meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics")
                )
            ],
        )
        metrics.set_meter_provider(meter_provider)

    telemetry = OtelTelemetry(
        tracer=trace.get_tracer("agent_workbench"),
        # ``metrics.get_meter`` returns a no-op meter when no provider is set,
        # which is what "traces but no metrics" should mean.
        meter=metrics.get_meter("agent_workbench"),
    )
    return telemetry, tracer_provider, meter_provider


def shutdown(
    tracer_provider: TracerProvider, meter_provider: MeterProvider | None
) -> None:
    """Flush and stop, without letting a collector delay a process exit forever.

    Suppressed for the same reason every method above is: a shutdown that
    raises turns a clean exit into a stack trace about telemetry.
    """

    with suppress(Exception):
        tracer_provider.shutdown()
    if meter_provider is not None:
        with suppress(Exception):
            meter_provider.shutdown()


__all__ = ["OtelTelemetry", "build_otel_telemetry", "shutdown"]
