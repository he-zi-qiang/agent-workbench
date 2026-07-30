"""Test-only adapters; production composition never imports their controller."""

from agent_workbench.adapters.testing.fault_injector import (
    AsyncBarrier,
    FailpointController,
    InjectedCrash,
    InjectedFaultError,
    NoopFaultInjector,
)
from agent_workbench.adapters.testing.stack import FakeStack, fake_stack

__all__ = [
    "AsyncBarrier",
    "FailpointController",
    "FakeStack",
    "InjectedCrash",
    "InjectedFaultError",
    "NoopFaultInjector",
    "fake_stack",
]
