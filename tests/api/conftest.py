"""Fixtures shared by the API tests."""

from __future__ import annotations

import os

import pytest

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"


@pytest.fixture
def events_dsn() -> str:
    """The DSN, not an engine.

    An engine is bound to the event loop it was created on, so handing one
    across ``asyncio.run`` boundaries leaves connections attached to a loop
    that has already closed. Each test opens its own inside its own loop.
    """

    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn
