"""Contract for the conversation store: ordering and tenant scoping.

Every test runs against both the in-memory store and PostgreSQL. Where the two
could differ is precisely where the interesting behaviour is -- position
assignment under concurrency, and what a wrong tenant is allowed to learn.
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

import pytest
from harness import StoreHarness

from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.messages import assistant_message, user_message
from agent_workbench.ports.conversation_store import (
    ConversationStore,
    StoredMessage,
    WorkspacePointerConflictError,
)

SESSION = "session_1"
CODE_SESSION = "session_code_1"
TENANT = "tenant_a"
OTHER_TENANT = "tenant_b"
OWNER = "user_1"
NEIGHBOUR = "user_2"
PROJECT = "prj_review"


@runtime_checkable
class _ProjectFiling(Protocol):
    """Filing a session under a project, done by each store's own ProjectStore.

    Not part of `ConversationStore`: this store never writes `project_id`, it
    only carries it back out. The seam is in `conftest`, and it goes through
    `ProjectStore.assign_session` on both sides.
    """

    async def file_under_project_for_test(
        self, *, tenant_id: str, owner_id: str, session_id: str, project_id: str
    ) -> None: ...


async def _filed_code_session(store: ConversationStore) -> None:
    await store.create_session(
        session_id=CODE_SESSION,
        tenant_id=TENANT,
        owner_id=OWNER,
        title="草稿",
        mode="code",
    )
    assert isinstance(store, _ProjectFiling)
    await store.file_under_project_for_test(
        tenant_id=TENANT,
        owner_id=OWNER,
        session_id=CODE_SESSION,
        project_id=PROJECT,
    )


async def _with_session(store: ConversationStore) -> ConversationStore:
    await store.create_session(session_id=SESSION, tenant_id=TENANT, owner_id=OWNER)
    return store


def test_messages_keep_a_monotonic_position(conversations: StoreHarness) -> None:
    async def scenario(store: ConversationStore) -> list[int]:
        await _with_session(store)
        await store.append(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            messages=(user_message("one"), assistant_message(text="two")),
        )
        await store.append(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            messages=(user_message("three"),),
        )
        history = await store.history(
            session_id=SESSION, tenant_id=TENANT, principal_id=OWNER
        )
        return [stored.sequence for stored in history]

    assert conversations.run(scenario) == [1, 2, 3]


def test_history_returns_the_messages_in_order(conversations: StoreHarness) -> None:
    async def scenario(store: ConversationStore) -> list[str]:
        await _with_session(store)
        await store.append(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            messages=(user_message("one"), assistant_message(text="two")),
        )
        history = await store.history(
            session_id=SESSION, tenant_id=TENANT, principal_id=OWNER
        )
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
            principal_id=OWNER,
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
            principal_id=OWNER,
            messages=(original,),
        )
        history = await store.history(
            session_id=SESSION, tenant_id=TENANT, principal_id=OWNER
        )
        return history[0].message == original

    assert conversations.run(scenario) is True


def test_another_tenant_cannot_read_the_session(conversations: StoreHarness) -> None:
    async def scenario(store: ConversationStore) -> None:
        await _with_session(store)
        await store.history(
            session_id=SESSION, tenant_id=OTHER_TENANT, principal_id=OWNER
        )

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
            principal_id=OWNER,
            messages=(user_message("injected"),),
        )

    with pytest.raises(NotFoundError):
        conversations.run(scenario)


def test_an_unknown_session_is_not_found(conversations: StoreHarness) -> None:
    async def scenario(store: ConversationStore) -> None:
        await _with_session(store)
        await store.history(
            session_id="session_missing", tenant_id=TENANT, principal_id=OWNER
        )

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
            await store.history(
                session_id=SESSION, tenant_id=OTHER_TENANT, principal_id=OWNER
            )
        except NotFoundError as exc:
            wrong_tenant = str(exc)
        try:
            await store.history(
                session_id="session_missing", tenant_id=TENANT, principal_id=OWNER
            )
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
            principal_id=OWNER,
            messages=(user_message("one"), user_message("two"), user_message("three")),
        )
        return len(
            await store.history(
                session_id=SESSION, tenant_id=TENANT, principal_id=OWNER, limit=2
            )
        )

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
                    principal_id=OWNER,
                    messages=(user_message(f"turn {index}"),),
                )
                for index in range(5)
            )
        )
        history = await store.history(
            session_id=SESSION, tenant_id=TENANT, principal_id=OWNER
        )
        return [stored.sequence for stored in history]

    assert conversations.run(scenario) == [1, 2, 3, 4, 5]


# --- one tenant, two people --------------------------------------------------


def test_a_neighbour_cannot_read_the_conversation(conversations: StoreHarness) -> None:
    """A conversation is the most personal thing this system stores.

    Scoping it to a tenant says whose database it is, not whose conversation it
    is -- and a session id travels through URLs and logs like any other id.
    """

    async def scenario(store: ConversationStore) -> None:
        await _with_session(store)
        await store.append(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            messages=(user_message("my private question"),),
        )
        await store.history(
            session_id=SESSION, tenant_id=TENANT, principal_id=NEIGHBOUR
        )

    with pytest.raises(NotFoundError):
        conversations.run(scenario)


def test_a_neighbour_cannot_append_to_the_conversation(
    conversations: StoreHarness,
) -> None:
    """Appending puts words in a history its owner will read back as their own."""

    async def scenario(store: ConversationStore) -> None:
        await _with_session(store)
        await store.append(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=NEIGHBOUR,
            messages=(user_message("injected"),),
        )

    with pytest.raises(NotFoundError):
        conversations.run(scenario)


def test_the_refusal_matches_a_missing_session_exactly(
    conversations: StoreHarness,
) -> None:
    """Three refusals, one message: not yours, not your tenant's, not there."""

    async def scenario(store: ConversationStore) -> list[str]:
        await _with_session(store)
        outcomes: list[str] = []
        for tenant, principal, session in (
            (TENANT, NEIGHBOUR, SESSION),
            (OTHER_TENANT, OWNER, SESSION),
            (TENANT, OWNER, "ses_00000000000000000000000000000"),
        ):
            try:
                await store.history(
                    session_id=session, tenant_id=tenant, principal_id=principal
                )
            except NotFoundError as refusal:
                outcomes.append(str(refusal))
            else:
                outcomes.append("allowed")
        return outcomes

    assert conversations.run(scenario) == ["conversation session not found"] * 3


# --- one session, one mode ---------------------------------------------------
#
# Chat and Code share this table because they share an identity: one principal,
# one tenant, one ordered history. They do not share a lifecycle -- Chat
# publishes an answer through a turn ledger and Code writes no turn row at all
# -- so a session id that let either API drive either kind of session would be
# a session whose lifecycle depends on which URL last touched it.


def test_a_code_session_is_not_a_chat_session(conversations: StoreHarness) -> None:
    async def scenario(store: ConversationStore) -> None:
        await store.create_session(
            session_id=CODE_SESSION,
            tenant_id=TENANT,
            owner_id=OWNER,
            mode="code",
        )
        await store.history(
            session_id=CODE_SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            mode="chat",
        )

    with pytest.raises(NotFoundError):
        conversations.run(scenario)


def test_a_chat_session_is_not_a_code_session(conversations: StoreHarness) -> None:
    """The gate swings both ways, or it is a rule about one API's manners."""

    async def scenario(store: ConversationStore) -> None:
        await _with_session(store)
        await store.history(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            mode="code",
        )

    with pytest.raises(NotFoundError):
        conversations.run(scenario)


def test_each_mode_still_reads_its_own(conversations: StoreHarness) -> None:
    """The control: the refusal is about the mode, not about reading at all.

    Without this, a ``history`` that refused every caller who named a mode
    would pass both tests above.
    """

    async def scenario(store: ConversationStore) -> tuple[list[str], list[str]]:
        await _with_session(store)
        await store.create_session(
            session_id=CODE_SESSION,
            tenant_id=TENANT,
            owner_id=OWNER,
            mode="code",
        )
        await store.append(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            messages=(user_message("a question"),),
        )
        await store.append(
            session_id=CODE_SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            messages=(user_message("edit the file"),),
        )
        chat = await store.history(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            mode="chat",
        )
        code = await store.history(
            session_id=CODE_SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            mode="code",
        )
        return (
            [stored.message.text() for stored in chat],
            [stored.message.text() for stored in code],
        )

    assert conversations.run(scenario) == (["a question"], ["edit the file"])


def test_a_session_defaults_to_chat(conversations: StoreHarness) -> None:
    """Every session written before the column existed was a chat session."""

    async def scenario(store: ConversationStore) -> tuple[str, int]:
        created = (
            await store.create_session(
                session_id=SESSION, tenant_id=TENANT, owner_id=OWNER
            )
        ).mode
        stored = await store.history(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            mode="chat",
        )
        return created, len(stored)

    assert conversations.run(scenario) == ("chat", 0)


def test_the_wrong_mode_answers_exactly_like_a_missing_session(
    conversations: StoreHarness,
) -> None:
    """ "That id exists, it is just not yours to drive" is still a disclosure.

    A distinguishable refusal turns the Chat API into an oracle for which of a
    caller's guessed session ids are real.
    """

    async def scenario(store: ConversationStore) -> tuple[str, str]:
        await store.create_session(
            session_id=CODE_SESSION,
            tenant_id=TENANT,
            owner_id=OWNER,
            mode="code",
        )
        wrong_mode = ""
        missing = ""
        try:
            await store.history(
                session_id=CODE_SESSION,
                tenant_id=TENANT,
                principal_id=OWNER,
                mode="chat",
            )
        except NotFoundError as refusal:
            wrong_mode = str(refusal)
        try:
            await store.history(
                session_id="ses_00000000000000000000000000000",
                tenant_id=TENANT,
                principal_id=OWNER,
                mode="chat",
            )
        except NotFoundError as refusal:
            missing = str(refusal)
        return wrong_mode, missing

    wrong_mode, missing = conversations.run(scenario)

    assert wrong_mode == missing == "conversation session not found"


def test_a_caller_that_names_no_mode_reads_either(
    conversations: StoreHarness,
) -> None:
    """The mode is a caller's declaration, not an ambient filter.

    Nothing in the store decides which mode a reader belongs to, so a reader
    that names none -- the release recovery scans, the expiration reaper's
    lookups -- keeps seeing exactly what it saw before this column existed.
    """

    async def scenario(store: ConversationStore) -> int:
        await store.create_session(
            session_id=CODE_SESSION,
            tenant_id=TENANT,
            owner_id=OWNER,
            mode="code",
        )
        await store.append(
            session_id=CODE_SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            messages=(user_message("edit the file"),),
        )
        return len(
            await store.history(
                session_id=CODE_SESSION,
                tenant_id=TENANT,
                principal_id=OWNER,
            )
        )

    assert conversations.run(scenario) == 1


def test_the_owner_still_reads_their_own(conversations: StoreHarness) -> None:
    """The control: the refusal is about who is asking, not about reading."""

    async def scenario(store: ConversationStore) -> int:
        await _with_session(store)
        await store.append(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            messages=(user_message("my private question"),),
        )
        return len(
            await store.history(
                session_id=SESSION, tenant_id=TENANT, principal_id=OWNER
            )
        )

    assert conversations.run(scenario) == 1


# --- the workspace pointer ---------------------------------------------------
#
# A Task carries its workspace version through graph state, so a dead attempt
# publishes nothing and its writes stay unreachable. A session has no graph:
# this column is where the version lives between turns, and the comparison is
# what stops two runs on one session from each publishing a manifest that names
# only its own files.


async def _advance(
    store: ConversationStore,
    *,
    expected: str | None,
    next_version: str,
    session_id: str = SESSION,
    principal_id: str = OWNER,
    tenant_id: str = TENANT,
) -> None:
    await store.advance_workspace_version(
        session_id=session_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        expected=expected,
        next_version=next_version,
    )


async def _pointer(store: ConversationStore) -> str | None:
    session = await store.session(
        session_id=SESSION, tenant_id=TENANT, principal_id=OWNER
    )
    return session.workspace_version


def test_a_new_session_has_written_nothing(conversations: StoreHarness) -> None:
    """``None`` is the starting state, and it is a value rather than a gap."""

    async def scenario(store: ConversationStore) -> str | None:
        await _with_session(store)
        return await _pointer(store)

    assert conversations.run(scenario) is None


def test_the_first_write_compares_against_nothing(
    conversations: StoreHarness,
) -> None:
    """The NULL case, which is every session's first write.

    Under ``=`` this comparison would be against NULL, match no row, and be
    reported as a race the caller was not in -- so this is the test that tells
    ``IS NOT DISTINCT FROM`` apart from equality.
    """

    async def scenario(store: ConversationStore) -> str | None:
        await _with_session(store)
        await _advance(store, expected=None, next_version="art_one")
        return await _pointer(store)

    assert conversations.run(scenario) == "art_one"


def test_the_pointer_moves_from_where_the_writer_left_it(
    conversations: StoreHarness,
) -> None:
    """The control for the refusal below: advancing in step keeps working."""

    async def scenario(store: ConversationStore) -> str | None:
        await _with_session(store)
        await _advance(store, expected=None, next_version="art_one")
        await _advance(store, expected="art_one", next_version="art_two")
        return await _pointer(store)

    assert conversations.run(scenario) == "art_two"


def test_a_stale_version_is_refused(conversations: StoreHarness) -> None:
    async def scenario(store: ConversationStore) -> None:
        await _with_session(store)
        await _advance(store, expected=None, next_version="art_one")
        # A second run that read the session before the first one wrote.
        await _advance(store, expected=None, next_version="art_other")

    with pytest.raises(WorkspacePointerConflictError):
        conversations.run(scenario)


def test_a_refused_advance_leaves_the_pointer_alone(
    conversations: StoreHarness,
) -> None:
    """Refusing is only half of it: the loser must not have moved anything.

    An implementation that wrote first and compared afterwards would raise
    here too, so the exception alone cannot tell the two apart.
    """

    async def scenario(store: ConversationStore) -> str | None:
        await _with_session(store)
        await _advance(store, expected=None, next_version="art_one")
        with pytest.raises(WorkspacePointerConflictError):
            await _advance(store, expected=None, next_version="art_other")
        return await _pointer(store)

    assert conversations.run(scenario) == "art_one"


def test_another_principal_cannot_move_the_pointer(
    conversations: StoreHarness,
) -> None:
    """And is told it does not exist, not that it lost a race.

    A conflict would confirm the session is real and say what version it is
    at -- the same leak the history methods refuse.
    """

    async def scenario(store: ConversationStore) -> None:
        await _with_session(store)
        await _advance(
            store, expected=None, next_version="art_one", principal_id=NEIGHBOUR
        )

    with pytest.raises(NotFoundError):
        conversations.run(scenario)


def test_another_tenant_cannot_read_the_pointer(conversations: StoreHarness) -> None:
    async def scenario(store: ConversationStore) -> None:
        await _with_session(store)
        await store.session(
            session_id=SESSION, tenant_id=OTHER_TENANT, principal_id=OWNER
        )

    with pytest.raises(NotFoundError):
        conversations.run(scenario)


# --- naming and listing -------------------------------------------------
#
# Both stores, because the two answer differently in exactly the places that
# matter here: a frozen model that one adapter has to copy and the other
# rewrites with SQL, and an ordering one computes in Python and the other in an
# index.


def test_a_principal_sees_only_their_own_sessions(conversations: StoreHarness) -> None:
    async def scenario(store: ConversationStore) -> list[str]:
        await store.create_session(
            session_id="ses_mine", tenant_id=TENANT, owner_id=OWNER, mode="code"
        )
        await store.create_session(
            session_id="ses_theirs", tenant_id=TENANT, owner_id=NEIGHBOUR, mode="code"
        )
        listed = await store.list_sessions(
            tenant_id=TENANT, principal_id=OWNER, mode="code"
        )
        return [session.session_id for session in listed]

    assert conversations.run(scenario) == ["ses_mine"]


def test_a_session_list_is_scoped_to_one_mode(conversations: StoreHarness) -> None:
    """Both directions. One alone would pass for a store that returns nothing."""

    async def scenario(store: ConversationStore) -> tuple[list[str], list[str]]:
        await store.create_session(
            session_id="ses_chat", tenant_id=TENANT, owner_id=OWNER, mode="chat"
        )
        await store.create_session(
            session_id="ses_code", tenant_id=TENANT, owner_id=OWNER, mode="code"
        )
        code = await store.list_sessions(
            tenant_id=TENANT, principal_id=OWNER, mode="code"
        )
        chat = await store.list_sessions(
            tenant_id=TENANT, principal_id=OWNER, mode="chat"
        )
        return (
            [session.session_id for session in code],
            [session.session_id for session in chat],
        )

    assert conversations.run(scenario) == (["ses_code"], ["ses_chat"])


def test_the_most_recently_spoken_in_session_comes_first(
    conversations: StoreHarness,
) -> None:
    """The ordering is activity, not creation -- and activity means messages.

    This is the test that pins the touch into `_append_messages` rather than
    into `append`: the older session is spoken in *after* the newer one was
    created, so a store ordering by creation puts them the other way round.
    """

    async def scenario(store: ConversationStore) -> list[str]:
        await store.create_session(
            session_id="ses_older", tenant_id=TENANT, owner_id=OWNER, mode="code"
        )
        await store.create_session(
            session_id="ses_newer", tenant_id=TENANT, owner_id=OWNER, mode="code"
        )
        await store.append(
            session_id="ses_older",
            tenant_id=TENANT,
            principal_id=OWNER,
            messages=(user_message("still here"),),
        )
        listed = await store.list_sessions(
            tenant_id=TENANT, principal_id=OWNER, mode="code"
        )
        return [session.session_id for session in listed]

    assert conversations.run(scenario) == ["ses_older", "ses_newer"]


def test_a_title_is_only_taken_when_there_is_none(
    conversations: StoreHarness,
) -> None:
    async def scenario(store: ConversationStore) -> tuple[str | None, str | None]:
        await store.create_session(
            session_id=CODE_SESSION, tenant_id=TENANT, owner_id=OWNER, mode="code"
        )
        await store.set_title_if_unset(
            session_id=CODE_SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            title="first instruction",
        )
        await store.set_title_if_unset(
            session_id=CODE_SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            title="second instruction",
        )
        after_two = await store.session(
            session_id=CODE_SESSION, tenant_id=TENANT, principal_id=OWNER
        )
        # And the one call that is allowed to overwrite still does.
        await store.rename_session(
            session_id=CODE_SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            title="what a person called it",
        )
        renamed = await store.session(
            session_id=CODE_SESSION, tenant_id=TENANT, principal_id=OWNER
        )
        return after_two.title, renamed.title

    assert conversations.run(scenario) == (
        "first instruction",
        "what a person called it",
    )


def test_renaming_somebody_elses_session_is_not_found(
    conversations: StoreHarness,
) -> None:
    """And the row its owner reads back is unchanged."""

    async def scenario(store: ConversationStore) -> tuple[bool, str | None]:
        await store.create_session(
            session_id=CODE_SESSION,
            tenant_id=TENANT,
            owner_id=OWNER,
            title="mine",
            mode="code",
        )
        try:
            await store.rename_session(
                session_id=CODE_SESSION,
                tenant_id=TENANT,
                principal_id=NEIGHBOUR,
                title="theirs now",
            )
            refused = False
        except NotFoundError:
            refused = True
        # The second half is what makes this more than a status assertion: a
        # store that wrote and then raised would satisfy the first half alone.
        owned = await store.session(
            session_id=CODE_SESSION, tenant_id=TENANT, principal_id=OWNER
        )
        return refused, owned.title

    assert conversations.run(scenario) == (True, "mine")


def test_renaming_your_own_session_returns_the_new_title(
    conversations: StoreHarness,
) -> None:
    """The control for the refusal above."""

    async def scenario(store: ConversationStore) -> str | None:
        await store.create_session(
            session_id=CODE_SESSION,
            tenant_id=TENANT,
            owner_id=OWNER,
            title="mine",
            mode="code",
        )
        renamed = await store.rename_session(
            session_id=CODE_SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            title="a better name",
        )
        return renamed.title

    assert conversations.run(scenario) == "a better name"


# --------------------------------------------------------------------------
# Deleting a session (ADR-056)
# --------------------------------------------------------------------------


def test_deleting_a_session_removes_it_and_its_messages(
    conversations: StoreHarness,
) -> None:
    """The row and the transcript go together, and the neighbour stays."""

    async def scenario(store: ConversationStore) -> tuple[bool, int]:
        for session_id in (SESSION, CODE_SESSION):
            await store.create_session(
                session_id=session_id,
                tenant_id=TENANT,
                owner_id=OWNER,
                title="mine",
                mode="chat" if session_id == SESSION else "code",
            )
            await store.append(
                session_id=session_id,
                tenant_id=TENANT,
                principal_id=OWNER,
                messages=(user_message("one"),),
            )

        await store.delete_session(
            session_id=SESSION, tenant_id=TENANT, principal_id=OWNER, mode="chat"
        )

        try:
            await store.session(
                session_id=SESSION, tenant_id=TENANT, principal_id=OWNER
            )
            gone = False
        except NotFoundError:
            gone = True

        # The second half is the one that would catch a delete written with
        # too wide a `WHERE`: one session leaving must not take the other's
        # transcript with it.
        survivor = await store.history(
            session_id=CODE_SESSION, tenant_id=TENANT, principal_id=OWNER
        )
        return gone, len(survivor)

    assert conversations.run(scenario) == (True, 1)


def test_deleting_somebody_elses_session_is_not_found(
    conversations: StoreHarness,
) -> None:
    """And the row its owner reads back is still there."""

    async def scenario(store: ConversationStore) -> tuple[bool, str | None]:
        await store.create_session(
            session_id=CODE_SESSION,
            tenant_id=TENANT,
            owner_id=OWNER,
            title="mine",
            mode="code",
        )
        try:
            await store.delete_session(
                session_id=CODE_SESSION,
                tenant_id=TENANT,
                principal_id=NEIGHBOUR,
                mode="code",
            )
            refused = False
        except NotFoundError:
            refused = True
        # A store that deleted and then raised would satisfy the first half.
        owned = await store.session(
            session_id=CODE_SESSION, tenant_id=TENANT, principal_id=OWNER
        )
        return refused, owned.title

    assert conversations.run(scenario) == (True, "mine")


def test_deleting_across_modes_is_not_found(conversations: StoreHarness) -> None:
    """A code session is not reachable through the chat service's delete.

    The third axis, asserted for delete the way it already is for every read
    here: mode is part of "is this yours", not a filter applied afterwards.
    """

    async def scenario(store: ConversationStore) -> tuple[bool, str | None]:
        await store.create_session(
            session_id=CODE_SESSION,
            tenant_id=TENANT,
            owner_id=OWNER,
            title="mine",
            mode="code",
        )
        try:
            await store.delete_session(
                session_id=CODE_SESSION,
                tenant_id=TENANT,
                principal_id=OWNER,
                mode="chat",
            )
            refused = False
        except NotFoundError:
            refused = True
        owned = await store.session(
            session_id=CODE_SESSION, tenant_id=TENANT, principal_id=OWNER
        )
        return refused, owned.title

    assert conversations.run(scenario) == (True, "mine")


# --- a session's project survives every projection that returns one ---------
#
# Three reads hand a caller a `ConversationSession`, and all three built it from
# a hand-written column list that omitted `project_id`. Nothing raised: the
# field has a default, so `model_validate` filled it and every answer said the
# session belonged to no project -- confidently. `session()` is the expensive
# one: `code_session.py::_project_files_for` reads it to decide what a Code turn
# is looking at, so every turn fell back to the flat workspace and was handed
# `CODE_TOOLS`, and the project tools of ADR-072/073/074 had never once reached
# a model.
#
# These live in the contract rather than only in `tests/persistence` because the
# in-memory double can now hold the fact: `InMemoryProjectStore` wired to an
# `InMemoryConversationStore` writes `project_id` onto the session object, the
# way `assign_session` writes the column. Before that it kept membership in its
# own `_members` dict, so the double answered `None` from the default too -- it
# agreed with the bug by construction, and a test parameterised over both would
# have passed against the implementation that had it.


def test_a_filed_session_still_says_so_when_one_turn_reads_it(
    conversations: StoreHarness,
) -> None:
    async def scenario(store: ConversationStore) -> str | None:
        await _filed_code_session(store)
        session = await store.session(
            session_id=CODE_SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            mode="code",
        )
        return session.project_id

    assert conversations.run(scenario) == PROJECT


def test_a_filed_session_still_says_so_when_it_is_listed(
    conversations: StoreHarness,
) -> None:
    async def scenario(store: ConversationStore) -> tuple[str | None, ...]:
        await _filed_code_session(store)
        listed = await store.list_sessions(
            tenant_id=TENANT, principal_id=OWNER, mode="code"
        )
        return tuple(session.project_id for session in listed)

    assert conversations.run(scenario) == (PROJECT,)


def test_renaming_a_filed_session_does_not_unfile_it(
    conversations: StoreHarness,
) -> None:
    """The renamed session is handed straight back to the caller.

    So an omission here is not a stale read that the next request corrects: it
    is the answer to the request that just changed the row.
    """

    async def scenario(store: ConversationStore) -> tuple[str | None, str | None]:
        await _filed_code_session(store)
        renamed = await store.rename_session(
            session_id=CODE_SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            title="复盘",
        )
        return renamed.title, renamed.project_id

    assert conversations.run(scenario) == ("复盘", PROJECT)


def test_an_unfiled_session_reads_as_unfiled_everywhere(
    conversations: StoreHarness,
) -> None:
    """The control, and it is not a formality.

    Without it a projection that hardcoded the column to a constant would pass
    all three tests above -- and no project is the *normal* state here (ADR-071
    §5.3), so it has to be reported as itself rather than as anything else.
    """

    async def scenario(store: ConversationStore) -> tuple[str | None, ...]:
        await store.create_session(
            session_id=CODE_SESSION, tenant_id=TENANT, owner_id=OWNER, mode="code"
        )
        read = await store.session(
            session_id=CODE_SESSION, tenant_id=TENANT, principal_id=OWNER
        )
        listed = await store.list_sessions(
            tenant_id=TENANT, principal_id=OWNER, mode="code"
        )
        renamed = await store.rename_session(
            session_id=CODE_SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            title="复盘",
        )
        return read.project_id, listed[0].project_id, renamed.project_id

    assert conversations.run(scenario) == (None, None, None)
