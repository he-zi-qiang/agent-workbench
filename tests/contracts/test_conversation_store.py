"""Contract for the conversation store: ordering and tenant scoping."""

from __future__ import annotations

import asyncio

import pytest

from agent_workbench.adapters.memory import InMemoryConversationStore
from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.messages import assistant_message, user_message
from agent_workbench.ports.conversation_store import StoredMessage

SESSION = "session_1"
TENANT = "tenant_a"
OTHER_TENANT = "tenant_b"
OWNER = "user_1"


async def _store_with_session() -> InMemoryConversationStore:
    store = InMemoryConversationStore()
    await store.create_session(session_id=SESSION, tenant_id=TENANT, owner_id=OWNER)
    return store


def test_messages_keep_a_monotonic_position() -> None:
    async def scenario() -> list[int]:
        store = await _store_with_session()
        await store.append(
            session_id=SESSION,
            tenant_id=TENANT,
            messages=(user_message("one"), assistant_message(text="two")),
        )
        await store.append(
            session_id=SESSION,
            tenant_id=TENANT,
            messages=(user_message("three"),),
        )
        history = await store.history(session_id=SESSION, tenant_id=TENANT)
        return [stored.sequence for stored in history]

    assert asyncio.run(scenario()) == [1, 2, 3]


def test_history_returns_the_messages_in_order() -> None:
    async def scenario() -> list[str]:
        store = await _store_with_session()
        await store.append(
            session_id=SESSION,
            tenant_id=TENANT,
            messages=(user_message("one"), assistant_message(text="two")),
        )
        history = await store.history(session_id=SESSION, tenant_id=TENANT)
        return [stored.message.text() for stored in history]

    assert asyncio.run(scenario()) == ["one", "two"]


def test_stored_messages_are_individually_addressable() -> None:
    async def scenario() -> tuple[StoredMessage, ...]:
        store = await _store_with_session()
        return await store.append(
            session_id=SESSION,
            tenant_id=TENANT,
            messages=(user_message("one"),),
        )

    stored = asyncio.run(scenario())

    assert stored[0].message_id.startswith("msg_")
    assert stored[0].session_id == SESSION


def test_another_tenant_cannot_read_the_session() -> None:
    async def scenario() -> None:
        store = await _store_with_session()
        await store.history(session_id=SESSION, tenant_id=OTHER_TENANT)

    with pytest.raises(NotFoundError):
        asyncio.run(scenario())


def test_another_tenant_cannot_append_to_the_session() -> None:
    """A session that cannot be read must not be writable either."""

    async def scenario() -> None:
        store = await _store_with_session()
        await store.append(
            session_id=SESSION,
            tenant_id=OTHER_TENANT,
            messages=(user_message("injected"),),
        )

    with pytest.raises(NotFoundError):
        asyncio.run(scenario())


def test_an_unknown_session_is_not_found() -> None:
    async def scenario() -> None:
        store = await _store_with_session()
        await store.history(session_id="session_missing", tenant_id=TENANT)

    with pytest.raises(NotFoundError):
        asyncio.run(scenario())


def test_history_can_be_limited() -> None:
    async def scenario() -> int:
        store = await _store_with_session()
        await store.append(
            session_id=SESSION,
            tenant_id=TENANT,
            messages=(user_message("one"), user_message("two"), user_message("three")),
        )
        return len(await store.history(session_id=SESSION, tenant_id=TENANT, limit=2))

    assert asyncio.run(scenario()) == 2


def test_a_session_id_cannot_be_reused() -> None:
    async def scenario() -> None:
        store = await _store_with_session()
        await store.create_session(
            session_id=SESSION,
            tenant_id=OTHER_TENANT,
            owner_id=OWNER,
        )

    with pytest.raises(ValueError, match="already exists"):
        asyncio.run(scenario())
