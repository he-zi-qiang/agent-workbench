"""Contract for the project store: membership, ownership, and what delete means.

Every test runs against both the in-memory store and PostgreSQL. Where the two
could differ is where the interesting behaviour is: PostgreSQL releases
membership through ON DELETE SET NULL and refuses a cross-owner write through
code, while the double does both by hand -- so a divergence in either shows up
as a failure here rather than as a surprise in production.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from harness import StoreHarness

from agent_workbench.ports.projects import ProjectRecord

TENANT = "tenant_a"
OTHER_TENANT = "tenant_b"
OWNER = "user_1"
NEIGHBOUR = "user_2"
PROJECT = "prj_review"
SESSION = "ses_1"
CODE_SESSION = "ses_code_1"
TASK = "task_1"
BASE = "kb_1"

_NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _record(
    project_id: str = PROJECT,
    *,
    owner_id: str = OWNER,
    tenant_id: str = TENANT,
    name: str = "季度复盘",
    updated_at: datetime = _NOW,
) -> ProjectRecord:
    return ProjectRecord(
        project_id=project_id,
        tenant_id=tenant_id,
        owner_id=owner_id,
        name=name,
        created_at=_NOW,
        updated_at=updated_at,
    )


def test_a_project_is_readable_only_by_its_owner(projects: StoreHarness) -> None:
    async def scenario(store: object) -> tuple[object, object]:
        await store.create(_record())
        mine = await store.get(tenant_id=TENANT, owner_id=OWNER, project_id=PROJECT)
        theirs = await store.get(
            tenant_id=TENANT, owner_id=NEIGHBOUR, project_id=PROJECT
        )
        return (None if mine is None else mine.name), theirs

    assert projects.run(scenario) == ("季度复盘", None)


def test_another_tenant_cannot_read_it_either(projects: StoreHarness) -> None:
    async def scenario(store: object) -> object:
        await store.create(_record())
        return await store.get(
            tenant_id=OTHER_TENANT, owner_id=OWNER, project_id=PROJECT
        )

    assert projects.run(scenario) is None


def test_the_list_hides_archived_projects_but_the_deep_link_does_not(
    projects: StoreHarness,
) -> None:
    """Archiving is not a soft delete (ADR-071 2.5)."""

    async def scenario(store: object) -> tuple[list[str], bool]:
        await store.create(_record())
        await store.set_archived(
            tenant_id=TENANT, owner_id=OWNER, project_id=PROJECT, archived=True
        )
        listed = await store.list_for_owner(tenant_id=TENANT, owner_id=OWNER)
        still_readable = (
            await store.get(tenant_id=TENANT, owner_id=OWNER, project_id=PROJECT)
        ) is not None
        return [record.project_id for record in listed], still_readable

    assert projects.run(scenario) == ([], True)


def test_asking_for_archived_ones_puts_them_after_the_live_ones(
    projects: StoreHarness,
) -> None:
    async def scenario(store: object) -> list[str]:
        await store.create(_record("prj_live", updated_at=_NOW - timedelta(days=3)))
        await store.create(_record("prj_old"))
        await store.set_archived(
            tenant_id=TENANT, owner_id=OWNER, project_id="prj_old", archived=True
        )
        listed = await store.list_for_owner(
            tenant_id=TENANT, owner_id=OWNER, include_archived=True
        )
        return [record.project_id for record in listed]

    assert projects.run(scenario) == ["prj_live", "prj_old"]


def test_deleting_a_project_releases_its_members_rather_than_removing_them(
    projects: StoreHarness,
) -> None:
    """The whole point of ON DELETE SET NULL (ADR-071 2.2)."""

    async def scenario(store: object) -> tuple[bool, list[str], list[str]]:
        await store.seed_session_for_test(
            tenant_id=TENANT, owner_id=OWNER, session_id=SESSION
        )
        await store.create(_record())
        await store.assign_session(
            tenant_id=TENANT,
            owner_id=OWNER,
            session_id=SESSION,
            project_id=PROJECT,
        )
        before = await store.contents(
            tenant_id=TENANT, owner_id=OWNER, project_id=PROJECT
        )
        removed = await store.delete(
            tenant_id=TENANT, owner_id=OWNER, project_id=PROJECT
        )
        # The session is still there; it simply belongs to nothing now.
        survivors = await store.session_ids_for_test(tenant_id=TENANT)
        return (
            removed,
            [item.item_id for item in before.items],
            survivors,
        )

    assert projects.run(scenario) == (True, [SESSION], [SESSION])


def test_a_neighbour_cannot_delete_it(projects: StoreHarness) -> None:
    async def scenario(store: object) -> tuple[bool, bool]:
        await store.create(_record())
        refused = await store.delete(
            tenant_id=TENANT, owner_id=NEIGHBOUR, project_id=PROJECT
        )
        still_there = (
            await store.get(tenant_id=TENANT, owner_id=OWNER, project_id=PROJECT)
        ) is not None
        return refused, still_there

    assert projects.run(scenario) == (False, True)


def test_none_takes_something_out_of_its_project(projects: StoreHarness) -> None:
    """``project_id=None`` is *no membership*, not *field absent* (ADR-071 4)."""

    async def scenario(store: object) -> tuple[list[str], list[str]]:
        await store.seed_session_for_test(
            tenant_id=TENANT, owner_id=OWNER, session_id=SESSION
        )
        await store.create(_record())
        await store.assign_session(
            tenant_id=TENANT,
            owner_id=OWNER,
            session_id=SESSION,
            project_id=PROJECT,
        )
        filed = await store.contents(
            tenant_id=TENANT, owner_id=OWNER, project_id=PROJECT
        )
        await store.assign_session(
            tenant_id=TENANT, owner_id=OWNER, session_id=SESSION, project_id=None
        )
        released = await store.contents(
            tenant_id=TENANT, owner_id=OWNER, project_id=PROJECT
        )
        return (
            [item.item_id for item in filed.items],
            [item.item_id for item in released.items],
        )

    assert projects.run(scenario) == ([SESSION], [])


def test_a_session_cannot_be_filed_under_somebody_elses_project(
    projects: StoreHarness,
) -> None:
    """The foreign key constrains tenant and knows nothing about owner."""

    async def scenario(store: object) -> tuple[bool, list[str]]:
        await store.seed_session_for_test(
            tenant_id=TENANT, owner_id=OWNER, session_id=SESSION
        )
        await store.create(_record(owner_id=NEIGHBOUR))
        accepted = await store.assign_session(
            tenant_id=TENANT,
            owner_id=OWNER,
            session_id=SESSION,
            project_id=PROJECT,
        )
        theirs = await store.contents(
            tenant_id=TENANT, owner_id=NEIGHBOUR, project_id=PROJECT
        )
        return accepted, [item.item_id for item in theirs.items]

    assert projects.run(scenario) == (False, [])


def test_assigning_something_that_does_not_exist_changes_nothing(
    projects: StoreHarness,
) -> None:
    async def scenario(store: object) -> bool:
        await store.create(_record())
        return await store.assign_session(
            tenant_id=TENANT,
            owner_id=OWNER,
            session_id="ses_never_created",
            project_id=PROJECT,
        )

    assert projects.run(scenario) is False


def test_contents_carries_chat_code_task_and_knowledge_base(
    projects: StoreHarness,
) -> None:
    async def scenario(store: object) -> list[tuple[str, str]]:
        await store.seed_session_for_test(
            tenant_id=TENANT,
            owner_id=OWNER,
            session_id=SESSION,
            mode="chat",
            title="问过的问题",
            last_activity_at=_NOW,
        )
        await store.seed_session_for_test(
            tenant_id=TENANT,
            owner_id=OWNER,
            session_id=CODE_SESSION,
            mode="code",
            title="改过的脚本",
            last_activity_at=_NOW - timedelta(hours=1),
        )
        await store.seed_task_for_test(
            tenant_id=TENANT,
            owner_id=OWNER,
            task_id=TASK,
            objective_preview="导出一份报告",
            created_at=_NOW - timedelta(hours=2),
        )
        await store.seed_knowledge_base_for_test(
            tenant_id=TENANT, owner_id=OWNER, knowledge_base_id=BASE, name="产品手册"
        )
        await store.create(_record())
        for assign in (
            store.assign_session(
                tenant_id=TENANT,
                owner_id=OWNER,
                session_id=SESSION,
                project_id=PROJECT,
            ),
            store.assign_session(
                tenant_id=TENANT,
                owner_id=OWNER,
                session_id=CODE_SESSION,
                project_id=PROJECT,
            ),
            store.assign_task(
                tenant_id=TENANT, owner_id=OWNER, task_id=TASK, project_id=PROJECT
            ),
        ):
            assert await assign is True
        assert (
            await store.link_knowledge_base(
                tenant_id=TENANT,
                owner_id=OWNER,
                project_id=PROJECT,
                knowledge_base_id=BASE,
            )
            is True
        )
        found = await store.contents(
            tenant_id=TENANT, owner_id=OWNER, project_id=PROJECT
        )
        return sorted((item.kind, item.item_id) for item in found.items)

    assert projects.run(scenario) == [
        ("chat", SESSION),
        ("code", CODE_SESSION),
        ("knowledge_base", BASE),
        ("task", TASK),
    ]


def test_linking_the_same_knowledge_base_twice_is_idempotent(
    projects: StoreHarness,
) -> None:
    async def scenario(store: object) -> tuple[bool, int]:
        await store.seed_knowledge_base_for_test(
            tenant_id=TENANT, owner_id=OWNER, knowledge_base_id=BASE, name="产品手册"
        )
        await store.create(_record())
        for _ in range(2):
            again = await store.link_knowledge_base(
                tenant_id=TENANT,
                owner_id=OWNER,
                project_id=PROJECT,
                knowledge_base_id=BASE,
            )
        found = await store.contents(
            tenant_id=TENANT, owner_id=OWNER, project_id=PROJECT
        )
        return again, len(found.items)

    assert projects.run(scenario) == (True, 1)


def test_unlinking_leaves_the_knowledge_base_alone(projects: StoreHarness) -> None:
    async def scenario(store: object) -> tuple[bool, int, bool]:
        await store.seed_knowledge_base_for_test(
            tenant_id=TENANT, owner_id=OWNER, knowledge_base_id=BASE, name="产品手册"
        )
        await store.create(_record())
        await store.link_knowledge_base(
            tenant_id=TENANT,
            owner_id=OWNER,
            project_id=PROJECT,
            knowledge_base_id=BASE,
        )
        removed = await store.unlink_knowledge_base(
            tenant_id=TENANT,
            owner_id=OWNER,
            project_id=PROJECT,
            knowledge_base_id=BASE,
        )
        found = await store.contents(
            tenant_id=TENANT, owner_id=OWNER, project_id=PROJECT
        )
        base_survived = await store.knowledge_base_exists_for_test(
            tenant_id=TENANT, knowledge_base_id=BASE
        )
        return removed, len(found.items), base_survived

    assert projects.run(scenario) == (True, 0, True)


def test_renaming_returns_the_record_as_it_now_stands(projects: StoreHarness) -> None:
    async def scenario(store: object) -> tuple[str | None, object]:
        await store.create(_record())
        renamed = await store.rename(
            tenant_id=TENANT, owner_id=OWNER, project_id=PROJECT, name="改过的名字"
        )
        missing = await store.rename(
            tenant_id=TENANT,
            owner_id=OWNER,
            project_id="prj_never_created",
            name="无所谓",
        )
        return (None if renamed is None else renamed.name), missing

    assert projects.run(scenario) == ("改过的名字", None)


# --- ADR-072: the directory a project may be ---------------------------------
#
# `root_path` is the one column added since ADR-071, and the questions it raises
# are the ones this suite already answers for membership: is it owner-scoped, is
# NULL a real state, and does clearing it destroy anything.


def test_a_project_starts_with_no_directory(projects: StoreHarness) -> None:
    async def scenario(store: object) -> object:
        await store.create(_record())
        stored = await store.get(tenant_id=TENANT, owner_id=OWNER, project_id=PROJECT)
        return None if stored is None else stored.root_path

    # NULL is the normal state, not a migration artefact (ADR-072 §5.5). A
    # project created without one behaves exactly as every project did before
    # the column existed.
    assert projects.run(scenario) is None


def test_registering_a_directory_round_trips(projects: StoreHarness) -> None:
    async def scenario(store: object) -> object:
        await store.create(_record())
        await store.set_root_path(
            tenant_id=TENANT,
            owner_id=OWNER,
            project_id=PROJECT,
            root_path="/srv/projects/alpha",
        )
        stored = await store.get(tenant_id=TENANT, owner_id=OWNER, project_id=PROJECT)
        return None if stored is None else stored.root_path

    assert projects.run(scenario) == "/srv/projects/alpha"


def test_the_path_is_stored_as_given_not_resolved(projects: StoreHarness) -> None:
    async def scenario(store: object) -> object:
        await store.create(_record())
        await store.set_root_path(
            tenant_id=TENANT,
            owner_id=OWNER,
            project_id=PROJECT,
            root_path="/srv/link/../projects/alpha",
        )
        stored = await store.get(tenant_id=TENANT, owner_id=OWNER, project_id=PROJECT)
        return None if stored is None else stored.root_path

    # Deliberately unresolved. Resolution happens where a sandbox is built, on
    # the machine holding the disk; a resolved copy in a row would be a second,
    # staler answer that looks authoritative -- the link it went through can be
    # repointed, and the row would not notice.
    assert projects.run(scenario) == "/srv/link/../projects/alpha"


def test_clearing_the_directory_is_expressible(projects: StoreHarness) -> None:
    async def scenario(store: object) -> tuple[object, object]:
        await store.create(_record())
        await store.set_root_path(
            tenant_id=TENANT,
            owner_id=OWNER,
            project_id=PROJECT,
            root_path="/srv/projects/alpha",
        )
        cleared = await store.set_root_path(
            tenant_id=TENANT, owner_id=OWNER, project_id=PROJECT, root_path=None
        )
        stored = await store.get(tenant_id=TENANT, owner_id=OWNER, project_id=PROJECT)
        return (
            None if cleared is None else cleared.root_path,
            None if stored is None else stored.root_path,
        )

    # `None` means *no directory*, which has to be distinguishable from *the
    # field was not sent* -- otherwise "stop pointing this at that folder" has
    # no way of being said (ADR-071 §4, ADR-072 §5).
    assert projects.run(scenario) == (None, None)


def test_a_neighbour_cannot_register_a_directory(projects: StoreHarness) -> None:
    async def scenario(store: object) -> tuple[object, object]:
        await store.create(_record())
        refused = await store.set_root_path(
            tenant_id=TENANT,
            owner_id=NEIGHBOUR,
            project_id=PROJECT,
            root_path="/srv/projects/theirs",
        )
        stored = await store.get(tenant_id=TENANT, owner_id=OWNER, project_id=PROJECT)
        return refused, (None if stored is None else stored.root_path)

    # Pointing somebody else's project at a directory you chose would be the
    # sharpest form of the cross-owner write this suite already forbids: it
    # aims their agent at your disk.
    assert projects.run(scenario) == (None, None)


def test_two_projects_may_share_one_directory(projects: StoreHarness) -> None:
    async def scenario(store: object) -> tuple[object, object]:
        await store.create(_record())
        await store.create(_record("prj_rag", name="RAG 评测"))
        for project_id in (PROJECT, "prj_rag"):
            await store.set_root_path(
                tenant_id=TENANT,
                owner_id=OWNER,
                project_id=project_id,
                root_path="/srv/projects/alpha",
            )
        first = await store.get(tenant_id=TENANT, owner_id=OWNER, project_id=PROJECT)
        second = await store.get(tenant_id=TENANT, owner_id=OWNER, project_id="prj_rag")
        return (
            None if first is None else first.root_path,
            None if second is None else second.root_path,
        )

    # No UNIQUE on the column: "the migration" and "the RAG evaluation" can be
    # two pieces of work in one checkout. Uniqueness would encode "a directory
    # is a project", which is the container model ADR-071 rejected.
    assert projects.run(scenario) == (
        "/srv/projects/alpha",
        "/srv/projects/alpha",
    )


def test_deleting_a_project_does_not_touch_its_directory(
    projects: StoreHarness,
) -> None:
    async def scenario(store: object) -> tuple[object, object]:
        await store.create(_record())
        await store.set_root_path(
            tenant_id=TENANT,
            owner_id=OWNER,
            project_id=PROJECT,
            root_path="/srv/projects/alpha",
        )
        removed = await store.delete(
            tenant_id=TENANT, owner_id=OWNER, project_id=PROJECT
        )
        gone = await store.get(tenant_id=TENANT, owner_id=OWNER, project_id=PROJECT)
        return removed, gone

    # The row goes; the directory is not this store's to remove and no code
    # path here can. Same shape as ADR-071 §2.2 -- deleting a label never
    # deletes what it labelled, and here what it labelled is somebody's disk.
    assert projects.run(scenario) == (True, None)
