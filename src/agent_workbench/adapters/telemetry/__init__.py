"""Telemetry adapters. The core records against ``ports.telemetry``."""

from agent_workbench.adapters.telemetry.otel import (
    OtelTelemetry,
    build_otel_telemetry,
    shutdown,
)

__all__ = ["OtelTelemetry", "build_otel_telemetry", "shutdown"]
