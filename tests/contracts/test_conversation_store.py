"""Contract for the conversation store: ordering and tenant scoping.

Every test runs against both the in-memory store and PostgreSQL. Where the two
could differ is precisely where the interesting behaviour is -- position
assignment under concurrency, and what a wrong tenant is allowed to learn.
"""

from __future__ import annotations

import asyncio

import pytest
from harness import StoreHarness

from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.messages import assistant_message, user_message
from agent_workbench.ports.conversation_store import ConversationStore, StoredMessage

SESSION = "session_1"
TENANT = "tenant_a"
OTHER_TENANT = "tenant_b"
OWNER = "user_1"


async def _with_session(store: ConversationStore) -> ConversationStore:
    await store.create_session(session_id=SESSION, tenant_id=TENANT, owner_id=OWNER)
    return store


def test_messages_keep_a_monotonic_position(conversations: StoreHarness) -> None:
    async def scenario(store: ConversationStore) -> list[int]:
        await _with_session(store)
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

    assert conversations.run(scenario) == [1, 2, 3]


def test_history_returns_the_messages_in_order(conversations: StoreHarness) -> None:
    async def scenario(store: ConversationStore) -> list[str]:
        await _with_session(store)
        await store.append(
            session_id=SESSION,
            tenant_id=TENANT,
            messages=(user_message("one"), assistant_message(text="two")),
        )
        history = await store.history(session_id=SESSION, tenant_id=TENANT)
        return [stored.message.text() for stored in history]

    assert conversations.run(scenario) == ["one", "two"]


def test_stored_messages_are_individually_addressable(
    conversations: StoreHarness,
) -> None:
    async def scenario(store: ConversationStore) -> tuple[StoredMessage, ...]:
        await _with_session(store)
        return await store.append(
            session_id=SESSION,
            tenant_id=TENANT,
            messages=(user_message("one"),),
        )

    stored = conversations.run(scenario)

    assert stored[0].message_id.startswith("msg_")
    assert stored[0].session_id == SESSION


def test_a_message_survives_the_round_trip(conversations: StoreHarness) -> None:
    """History is replayed into a model call, so it must come back identical."""

    async def scenario(store: ConversationStore) -> bool:
        await _with_session(store)
        original = assistant_message(text="Qdrant owns fusion.")
        await store.append(
            session_id=SESSION,
            tenant_id=TENANT,
            messages=(original,),
        )
        history = await store.history(session_id=SESSION, tenant_id=TENANT)
        return history[0].message == original

    assert conversations.run(scenario) is True


def test_another_tenant_cannot_read_the_session(conversations: StoreHarness) -> None:
    async def scenario(store: ConversationStore) -> None:
        await _with_session(store)
        await store.history(session_id=SESSION, tenant_id=OTHER_TENANT)

    with pytest.raises(NotFoundError):
        conversations.run(scenario)


def test_another_tenant_cannot_append_to_the_session(
    conversations: StoreHarness,
) -> None:
    """A session that cannot be read must not be writable either."""

    async def scenario(store: ConversationStore) -> None:
        await _with_session(store)
        await store.append(
            session_id=SESSION,
            tenant_id=OTHER_TENANT,
            messages=(user_message("injected"),),
        )

    with pytest.raises(NotFoundError):
        conversations.run(scenario)


def test_an_unknown_session_is_not_found(conversations: StoreHarness) -> None:
    async def scenario(store: ConversationStore) -> None:
        await _with_session(store)
        await store.history(session_id="session_missing", tenant_id=TENANT)

    with pytest.raises(NotFoundError):
        conversations.run(scenario)


def test_a_wrong_tenant_and_a_missing_session_fail_identically(
    conversations: StoreHarness,
) -> None:
    """Telling them apart would confirm another tenant's session exists."""

    async def scenario(store: ConversationStore) -> tuple[str, str]:
        await _with_session(store)
        wrong_tenant = ""
        missing = ""
        try:
            await store.history(session_id=SESSION, tenant_id=OTHER_TENANT)
        except NotFoundError as exc:
            wrong_tenant = str(exc)
        try:
            await store.history(session_id="session_missing", tenant_id=TENANT)
        except NotFoundError as exc:
            missing = str(exc)
        return wrong_tenant, missing

    wrong_tenant, missing = conversations.run(scenario)

    assert wrong_tenant == missing


def test_history_can_be_limited(conversations: StoreHarness) -> None:
    async def scenario(store: ConversationStore) -> int:
        await _with_session(store)
        await store.append(
            session_id=SESSION,
            tenant_id=TENANT,
            messages=(user_message("one"), user_message("two"), user_message("three")),
        )
        return len(await store.history(session_id=SESSION, tenant_id=TENANT, limit=2))

    assert conversations.run(scenario) == 2


def test_a_session_id_cannot_be_reused(conversations: StoreHarness) -> None:
    async def scenario(store: ConversationStore) -> None:
        await _with_session(store)
        await store.create_session(
            session_id=SESSION,
            tenant_id=OTHER_TENANT,
            owner_id=OWNER,
        )

    with pytest.raises(ValueError, match="already exists"):
        conversations.run(scenario)


def test_concurrent_appends_never_reuse_a_position(
    conversations: StoreHarness,
) -> None:
    """The lock assigns positions; the unique constraint notices if it did not."""

    async def scenario(store: ConversationStore) -> list[int]:
        await _with_session(store)
        await asyncio.gather(
            *(
                store.append(
                    session_id=SESSION,
                    tenant_id=TENANT,
                    messages=(user_message(f"turn {index}"),),
                )
                for index in range(5)
            )
        )
        history = await store.history(session_id=SESSION, tenant_id=TENANT)
        return [stored.sequence for stored in history]

    assert conversations.run(scenario) == [1, 2, 3, 4, 5]
