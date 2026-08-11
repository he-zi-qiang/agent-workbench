"""Telemetry adapters. The core records against ``ports.telemetry``."""

from agent_workbench.adapters.telemetry.event_loop_lag import EventLoopLagWatchdog
from agent_workbench.adapters.telemetry.otel import (
    OtelTelemetry,
    build_otel_telemetry,
    shutdown,
)

__all__ = [
    "EventLoopLagWatchdog",
    "OtelTelemetry",
    "build_otel_telemetry",
    "shutdown",
]
