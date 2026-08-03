"""The manifest is only worth writing if it can be wrong out loud.

A file that lists paths and revisions is easy. What makes it evidence rather
than a summary is what it refuses to do: name a report that does not exist,
name a commit that does not describe the tree, or stay quiet about the pieces
nobody produced. Each of those is a test here, and each has its control group
in the same file -- the same call, one thing changed.

The last one is the interesting one. ``missing`` is derived from what was
attached, so there is no code path that can produce a manifest asserting a
completeness it does not have.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_workbench.bootstrap.evidence import (
    EVIDENCE_KINDS,
    EvidenceError,
    attach,
    build_manifest,
    main,
    repository_root,
    run,
    verify_manifest,
)
from agent_workbench.bootstrap.paths import TEST_CONFIG_FILE
from agent_workbench.bootstrap.settings import Settings, load_settings

ROOT = repository_root(Path(__file__).parent)


def _required_environment(monkeypatch: Any) -> None:
    dsn = "postgresql+asyncpg://agent:test@postgres:5432/agent_workbench"
    monkeypatch.setenv("AW_DATABASE__DSN", dsn)
    monkeypatch.setenv("AW_DATABASE__GUARD_DSN", dsn)
    monkeypatch.setenv("AW_DATABASE__LISTEN_DSN", dsn)


def _settings(tmp_path: Path) -> Settings:
    return load_settings(
        config_file=TEST_CONFIG_FILE,
        env_file=tmp_path / "missing.env",
    )


def _report(tmp_path: Path, name: str = "pytest.txt") -> Path:
    path = tmp_path / name
    path.write_text("1821 passed, 11 skipped\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# What it derives
# --------------------------------------------------------------------------


def test_the_derived_facts_are_the_ones_a_reader_cannot_reproduce_from_prose(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Revisions and fingerprints, not a description of them.

    Each of these identifies what the gate actually ran under: two manifests
    with the same numbers and different fingerprints were produced by different
    rule sets, which is exactly what a status document cannot tell you.
    """

    _required_environment(monkeypatch)

    manifest = build_manifest(
        gate="probe", settings=_settings(tmp_path), root=ROOT, allow_dirty=True
    )

    config = manifest["config"]
    assert config["startup_config_revision"]
    assert config["run_semantics_template_revision"]
    assert config["policy_revision_label"]
    assert len(config["canonical_policy_fingerprint"]) == 64
    assert config["graph_version"]
    assert config["model"]["provider"]
    assert config["embedding"]["revision"]
    assert config["reranker"]["revision"]
    assert config["qdrant_index"]["collection_schema_version"] >= 1
    assert len(manifest["git"]["git_commit"]) == 40


def test_the_manifest_carries_no_secret(monkeypatch: Any, tmp_path: Path) -> None:
    """It is committed and read by strangers.

    The control group is the config check's own canary test: that one proves
    the redacted snapshot is safe. This one proves the manifest never reaches
    for the snapshot at all.
    """

    _required_environment(monkeypatch)
    canary = "agent-workbench-evidence-secret-canary"
    monkeypatch.setenv("AW_SECRETS__DEEPSEEK_API_KEY", canary)

    manifest = build_manifest(
        gate="probe", settings=_settings(tmp_path), root=ROOT, allow_dirty=True
    )

    serialized = json.dumps(manifest)
    assert canary not in serialized
    assert "postgresql" not in serialized


# --------------------------------------------------------------------------
# What it refuses
# --------------------------------------------------------------------------


def test_an_attachment_that_does_not_exist_stops_the_manifest(tmp_path: Path) -> None:
    """A path is not a report.

    Recording it and letting the reader discover the gap is how a manifest ends
    up asserting evidence nobody produced. The control group is below: the same
    call with a real file attaches cleanly.
    """

    with pytest.raises(EvidenceError, match="does not exist"):
        attach("test_report", tmp_path / "never-written.txt")

    attached = attach("test_report", _report(tmp_path))
    assert attached.bytes > 0
    assert len(attached.sha256) == 64


def test_an_empty_attachment_is_refused_too(tmp_path: Path) -> None:
    """The most convincing kind of missing report.

    It has a path, a hash and a line in the manifest, and it says nothing.
    """

    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")

    with pytest.raises(EvidenceError, match="empty"):
        attach("test_report", empty)


def test_an_unknown_evidence_kind_is_refused(tmp_path: Path) -> None:
    """The vocabulary is fixed so that ``missing`` can mean something.

    A free-form kind would let a gate attach ``screenshots`` and appear to have
    covered a slot the manifest never tracked.
    """

    with pytest.raises(EvidenceError, match="unknown evidence kind"):
        attach("vibes", _report(tmp_path))


def test_a_dirty_tree_refuses_unless_it_is_recorded_as_dirty(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A manifest names a commit. If the tree differs, the commit is decoration.

    Both directions in one test, because the difference between them is the
    whole point: the refusal is not "you cannot do this", it is "say which kind
    of manifest this is".
    """

    _required_environment(monkeypatch)
    settings = _settings(tmp_path)
    scratch = tmp_path / "repo"
    scratch.mkdir()
    _init_repo(scratch)
    (scratch / "unstaged.txt").write_text("uncommitted\n", encoding="utf-8")

    with pytest.raises(EvidenceError, match="uncommitted changes"):
        build_manifest(gate="probe", settings=settings, root=scratch)

    provisional = build_manifest(
        gate="probe", settings=settings, root=scratch, allow_dirty=True
    )
    assert provisional["git"]["git_dirty"] is True


# --------------------------------------------------------------------------
# What it will not stay quiet about
# --------------------------------------------------------------------------


def test_everything_unattached_is_listed_as_missing(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Derived, never supplied.

    A gate is allowed to be incomplete. It is not allowed to be silent about
    it, and there is no argument that makes this list shorter than the truth.
    """

    _required_environment(monkeypatch)

    manifest = build_manifest(
        gate="probe",
        settings=_settings(tmp_path),
        root=ROOT,
        attachments=[attach("test_report", _report(tmp_path))],
        allow_dirty=True,
    )

    assert "test_report" not in manifest["missing"]
    assert set(manifest["missing"]) == {
        *(kind for kind in EVIDENCE_KINDS if kind != "test_report"),
        "task_run_semantics_revision",
    }


def test_a_task_snapshot_revision_is_recorded_when_a_run_produced_one(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The one revision configuration cannot derive.

    It belongs to a concrete Task row rather than to the settings a Task was
    submitted under, so it is supplied or it is missing -- never inferred.
    """

    _required_environment(monkeypatch)

    manifest = build_manifest(
        gate="probe",
        settings=_settings(tmp_path),
        root=ROOT,
        task_run_semantics_revision="1.3:v1.0:abc0123456789def",
        allow_dirty=True,
    )

    assert manifest["config"]["task_run_semantics_revision"] == (
        "1.3:v1.0:abc0123456789def"
    )
    assert "task_run_semantics_revision" not in manifest["missing"]


# --------------------------------------------------------------------------
# What makes it worth having written
# --------------------------------------------------------------------------


def test_verify_notices_a_report_that_changed_after_it_was_recorded(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A recorded hash nobody recomputes is a hash that was correct once.

    The control group is the first assertion: the same manifest verifies clean
    until the file underneath it moves.
    """

    _required_environment(monkeypatch)
    report = _report(tmp_path)
    exit_code = main(
        [
            "write",
            "--gate",
            "probe",
            "--config",
            str(TEST_CONFIG_FILE),
            "--env-file",
            str(tmp_path / "missing.env"),
            "--attach",
            f"test_report={report}",
            "--allow-dirty",
            "--out",
            str(tmp_path / "manifest.json"),
        ]
    )
    assert exit_code == 0

    manifest_path = tmp_path / "manifest.json"
    assert verify_manifest(manifest_path) == []

    report.write_text("1821 passed, 0 skipped\n", encoding="utf-8")
    problems = verify_manifest(manifest_path)

    assert len(problems) == 1
    assert "changed since recording" in problems[0]
    payload, code = run(["verify", str(manifest_path)])
    assert code == 1
    assert payload["status"] == "stale"


def test_a_refusal_exits_non_zero_and_writes_nothing(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    """The CLI's contract, since a build step is what usually calls it."""

    _required_environment(monkeypatch)
    out = tmp_path / "manifest.json"

    exit_code = main(
        [
            "write",
            "--gate",
            "probe",
            "--config",
            str(TEST_CONFIG_FILE),
            "--env-file",
            str(tmp_path / "missing.env"),
            "--attach",
            f"test_report={tmp_path / 'never-written.txt'}",
            "--allow-dirty",
            "--out",
            str(out),
        ]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "refused"
    assert not out.exists()


def _init_repo(path: Path) -> None:
    """A throwaway repository, so the dirty-tree test does not need a dirty one."""

    import subprocess

    for arguments in (
        ("init", "-q"),
        ("config", "user.email", "tests@example.invalid"),
        ("config", "user.name", "tests"),
        ("commit", "-q", "--allow-empty", "-m", "root"),
    ):
        subprocess.run(
            ["git", *arguments], cwd=path, check=True, capture_output=True, timeout=30
        )
