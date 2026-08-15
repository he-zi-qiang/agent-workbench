"""Contract for the cancellation token, including the wait both sides must honour.

Polling is enough for code that is doing something: it asks between steps. It
is no use at all to code that is waiting on somebody else, because the check
placed after the wait only runs once the wait ends -- and the whole problem is
that the wait may not end. So a token has to answer "tell me when" as well as
"has it happened", and both implementations have to answer it the same way:
one by waking, one by honestly never returning.

The waits here use real wall-clock timeouts. There is no clock to inject: the
parking is done by ``asyncio.Event``, so a test that wants to observe it has to
spend a little real time, and the numbers below are picked to be two orders of
magnitude apart rather than to be precise.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_workbench.domain.errors import OperationCancelledError
from agent_workbench.ports.cancellation import (
    CancellationSource,
    CancellationToken,
    NullCancellationToken,
)

#: Long enough that a woken waiter always makes it, short enough that a failure
#: is a failure rather than a hung suite.
WOKEN = 1.0
#: Short enough to stay a test, long enough that a waiter which was going to
#: return spuriously would have.
PARKED = 0.05


def _run(scenario: Any) -> Any:
    return asyncio.run(scenario())


def test_a_source_cancelled_before_anyone_waits_does_not_park() -> None:
    """The event is created by the waiter, so a cancel before that set nothing.

    Without the flag being checked first, this case parks on an event whose
    setter has already run and will not run again.
    """

    async def scenario() -> bool:
        source = CancellationSource()
        source.cancel("stopped early")
        await asyncio.wait_for(source.wait_cancelled(), timeout=WOKEN)
        return True

    assert _run(scenario) is True


def test_a_waiter_parked_before_the_cancel_is_woken_by_it() -> None:
    async def scenario() -> bool:
        source = CancellationSource()
        waiter = asyncio.ensure_future(source.wait_cancelled())
        # Yield so the waiter really parks; cancelling before it runs would
        # test the case above again rather than this one.
        await asyncio.sleep(0)
        assert not waiter.done()

        source.cancel("operator stopped the run")
        await asyncio.wait_for(waiter, timeout=WOKEN)
        return True

    assert _run(scenario) is True


def test_every_waiter_is_woken_and_not_only_the_first() -> None:
    """One approval wait and one loop-level watcher may race the same token."""

    async def scenario() -> list[bool]:
        source = CancellationSource()
        waiters = [asyncio.ensure_future(source.wait_cancelled()) for _ in range(3)]
        await asyncio.sleep(0)

        source.cancel()
        await asyncio.wait_for(asyncio.gather(*waiters), timeout=WOKEN)
        return [waiter.done() for waiter in waiters]

    assert _run(scenario) == [True, True, True]


def test_a_token_that_can_never_be_cancelled_never_returns() -> None:
    """The control, and the reason the docstring calls parking the honest answer.

    A ``NullCancellationToken`` whose wait returned would tell a racing caller
    that cancellation happened, which for this token is the one thing that
    cannot be true. The caller's obligation -- cancel the loser -- is what the
    finally clause here stands in for.
    """

    async def scenario() -> bool:
        waiter = asyncio.ensure_future(NullCancellationToken().wait_cancelled())
        try:
            await asyncio.wait({waiter}, timeout=PARKED)
            return waiter.done()
        finally:
            waiter.cancel()

    assert _run(scenario) is False


def test_a_source_built_outside_a_loop_still_wakes_its_waiter() -> None:
    """Where a source is constructed is not where it is awaited.

    The chat route builds one per request and composition builds others; an
    event created in ``__init__`` would bind to whatever loop was running
    then -- often none -- and fail later, somewhere else, for a reason that
    reads like anything but this.
    """

    source = CancellationSource()

    async def scenario() -> bool:
        waiter = asyncio.ensure_future(source.wait_cancelled())
        await asyncio.sleep(0)
        source.cancel("client_disconnected")
        await asyncio.wait_for(waiter, timeout=WOKEN)
        return True

    assert _run(scenario) is True


def test_the_checkpoint_answers_are_unchanged() -> None:
    """The control for "this cut adds a way to wait and changes nothing else"."""

    source = CancellationSource()
    null = NullCancellationToken()

    assert source.cancelled is False
    assert null.cancelled is False
    assert source.raise_if_cancelled() is None
    assert null.raise_if_cancelled() is None

    source.cancel("first reason")
    source.cancel("second reason")

    assert source.cancelled is True
    assert source.reason == "first reason"
    with pytest.raises(OperationCancelledError, match="first reason"):
        source.raise_if_cancelled()


@pytest.mark.parametrize(
    "token",
    [CancellationSource(), NullCancellationToken()],
    ids=["source", "null"],
)
def test_both_implementations_satisfy_the_protocol(token: CancellationToken) -> None:
    """Structural, not nominal: nothing inherits, so nothing enforces this."""

    assert isinstance(token, CancellationToken)
