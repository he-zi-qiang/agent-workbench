"""The evidence manifest for one release gate.

The implementation plan has required ``artifacts/evidence/<gate>/manifest.json``
since it was written, and nothing produced one. What existed instead was prose:
test counts in a status document, an evaluation number in a README, a claim
that a demo runs. Every one of those is true and none of them is checkable --
a reader cannot tell which commit produced a number, and neither can the person
who wrote it three weeks later.

So this tool records two kinds of thing and keeps them apart.

**Derived facts** come from the configuration and the repository: the startup
config revision, the run-semantics template revision, the policy label with its
canonical fingerprint, the graph version, the model, embedding and reranker
identities, the Qdrant index version, the commit. Nothing here is typed in by
hand, so nothing here can be wishful.

**Attachments** are files somebody produced -- a test report, an evaluation
report, a trace sample, a demo recording. Each is recorded with its SHA-256 and
its size, which is what makes ``verify`` possible: a manifest that points at a
report nobody can reproduce is a citation to a document that may since have
changed.

Two rules follow from what this is for, and both are refusals.

An attachment that does not exist stops the manifest from being written. The
alternative -- recording the path and letting the reader discover the gap -- is
how a manifest ends up asserting evidence that was never produced.

A dirty working tree stops it too, unless the caller says otherwise. A manifest
names a commit, and the numbers in it came from whatever was actually on disk;
if those differ, the commit is decoration. ``--allow-dirty`` exists for local
iteration and records ``git_dirty`` as true, so the manifest says which kind it
is rather than hiding it.

What is *not* attached is listed in ``missing``, and that list is derived rather
than supplied. A gate with no demo recording says so in its own manifest. This
is the difference between an evidence pack and a marketing page: the pack is
allowed to be incomplete, and is not allowed to be silent about it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from agent_workbench.bootstrap.config_check import PROFILE_CONFIG_FILES
from agent_workbench.bootstrap.settings import Settings, load_settings

#: Where a gate's manifest goes unless the caller says otherwise. Relative to
#: the repository root, because an evidence pack belongs beside the code it is
#: evidence for.
EVIDENCE_ROOT: Final[str] = "artifacts/evidence"

MANIFEST_NAME: Final[str] = "manifest.json"

#: The evidence a gate can carry. Each is a file rather than a claim, and each
#: one absent is reported in ``missing`` rather than omitted.
EVIDENCE_KINDS: Final[tuple[str, ...]] = (
    "test_report",
    "evaluation_report",
    "otel_trace_sample",
    "demo",
)

#: How much of a file is hashed. All of it -- a partial hash would verify a
#: prefix and miss an appended paragraph, which is the edit a report is most
#: likely to receive.
_CHUNK_BYTES: Final[int] = 1024 * 1024


class EvidenceError(RuntimeError):
    """A manifest could not be written, or no longer describes what it names.

    One type for both, because the caller's response is the same: stop, and
    show a person what is wrong. The message carries the detail; the exit code
    carries the verdict.
    """


@dataclass(frozen=True, slots=True)
class Attachment:
    """One file offered as evidence, and what it was when offered."""

    kind: str
    path: Path
    sha256: str
    bytes: int

    def as_json(self, *, root: Path) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": _display_path(self.path, root=root),
            "sha256": self.sha256,
            "bytes": self.bytes,
        }


def repository_root(start: Path | None = None) -> Path:
    """The git work tree this command is being run in.

    Asked of git rather than derived from ``__file__``: an installed package
    lives outside the repository, and a manifest that resolved its paths
    against site-packages would be unreadable to everyone but the machine that
    wrote it.
    """

    result = _git(("rev-parse", "--show-toplevel"), cwd=start or Path.cwd())
    return Path(result).resolve()


def git_state(root: Path) -> dict[str, Any]:
    """The commit, and whether the tree still matches it."""

    commit = _git(("rev-parse", "HEAD"), cwd=root)
    dirty = bool(_git(("status", "--porcelain"), cwd=root))
    return {"git_commit": commit, "git_dirty": dirty}


def digest(path: Path) -> tuple[str, int]:
    """SHA-256 and size, streamed. A trace sample can be large."""

    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            hasher.update(chunk)
            size += len(chunk)
    return hasher.hexdigest(), size


def attach(kind: str, path: Path) -> Attachment:
    """Hash one file, or refuse to pretend it exists."""

    if kind not in EVIDENCE_KINDS:
        raise EvidenceError(
            f"unknown evidence kind {kind!r}; expected one of "
            f"{', '.join(EVIDENCE_KINDS)}"
        )
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise EvidenceError(f"{kind} attachment does not exist: {path}")
    sha256, size = digest(resolved)
    if size == 0:
        # An empty report is the most convincing kind of missing one: it has a
        # path, a hash and a line in the manifest, and it says nothing.
        raise EvidenceError(f"{kind} attachment is empty: {path}")
    return Attachment(kind=kind, path=resolved, sha256=sha256, bytes=size)


def config_facts(settings: Settings) -> dict[str, Any]:
    """Everything the manifest can derive rather than be told.

    Revisions and fingerprints only. No DSN, no key, no endpoint header: this
    file is committed and read by strangers, and ``public_config`` exists for
    the case where somebody genuinely wants the whole redacted snapshot.
    """

    return {
        "environment": settings.app.environment,
        "config_schema_version": settings.app.config_schema_version,
        "architecture_baseline": settings.app.architecture_baseline,
        "startup_config_revision": settings.revision(),
        "run_semantics_template_revision": settings.run_semantics_revision(),
        "policy_revision_label": settings.policy.revision,
        "canonical_policy_fingerprint": settings.policy_fingerprint(),
        "graph_version": settings.workflow.graph_version,
        "model": {
            "provider": settings.model.provider,
            "main_model_id": settings.model.main.model_id,
            "compact_model_id": settings.model.compact.model_id,
        },
        "embedding": {
            "model_id": settings.rag.embedding.model_id,
            "revision": settings.rag.embedding.revision,
        },
        "reranker": {
            "model_id": settings.rag.reranker.model_id,
            "revision": settings.rag.reranker.revision,
        },
        "qdrant_index": {
            "read_alias": settings.qdrant.read_alias,
            "collection_schema_version": settings.qdrant.collection_schema_version,
            "distance": settings.qdrant.distance,
        },
    }


def build_manifest(
    *,
    gate: str,
    settings: Settings,
    root: Path,
    attachments: Sequence[Attachment] = (),
    task_run_semantics_revision: str | None = None,
    known_limitations: Sequence[str] = (),
    allow_dirty: bool = False,
) -> dict[str, Any]:
    """Assemble one gate's manifest, or refuse to.

    ``task_run_semantics_revision`` is the one revision this cannot derive: it
    belongs to a concrete Task row, not to the configuration a Task was
    submitted under. Supplied, it is recorded; absent, it is reported missing.
    """

    if not gate.strip():
        raise EvidenceError("a manifest belongs to a named gate")
    git = git_state(root)
    if git["git_dirty"] and not allow_dirty:
        raise EvidenceError(
            "the working tree has uncommitted changes, so the recorded commit "
            "would not describe what produced this evidence; commit first, or "
            "pass --allow-dirty to record it as provisional"
        )

    facts = config_facts(settings)
    facts["task_run_semantics_revision"] = task_run_semantics_revision
    supplied = {attachment.kind for attachment in attachments}
    missing = [kind for kind in EVIDENCE_KINDS if kind not in supplied]
    if task_run_semantics_revision is None:
        missing.append("task_run_semantics_revision")

    return {
        "gate": gate.strip(),
        "git": git,
        "config": facts,
        "attachments": [
            attachment.as_json(root=root)
            for attachment in sorted(attachments, key=lambda a: (a.kind, a.path))
        ],
        # Derived, never supplied. A gate that carries no demo says so here
        # rather than by omission, and nothing can produce a manifest that
        # claims completeness it does not have.
        "missing": missing,
        "known_limitations": [
            text.strip() for text in known_limitations if text.strip()
        ],
    }


def verify_manifest(path: Path) -> list[str]:
    """Re-hash what a manifest points at. Returns one line per problem.

    This is the half that makes the manifest worth writing. A recorded hash
    that nobody ever recomputes is a hash that was correct once.
    """

    manifest = json.loads(path.read_text(encoding="utf-8"))
    problems: list[str] = []
    # Resolved lazily, and only for the paths that need it: a manifest whose
    # attachments are all absolute is verifiable from anywhere, and asking git
    # about a directory that is not a work tree would fail before checking a
    # single hash.
    root: Path | None = None
    for entry in manifest.get("attachments", []):
        recorded = Path(str(entry["path"]))
        if recorded.is_absolute():
            target = recorded
        else:
            root = root if root is not None else repository_root(path.parent)
            target = (root / recorded).resolve()
        if not target.is_file():
            problems.append(f"{entry['kind']}: missing file {entry['path']}")
            continue
        sha256, size = digest(target)
        if sha256 != entry["sha256"]:
            problems.append(f"{entry['kind']}: {entry['path']} changed since recording")
        elif size != entry["bytes"]:  # pragma: no cover - implied by the digest
            problems.append(f"{entry['kind']}: {entry['path']} changed size")
    return problems


def _display_path(path: Path, *, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        # Outside the repository. Recorded as given rather than rewritten: a
        # path nobody else can resolve is better than one that looks resolvable
        # and is not.
        return str(path)


def _git(arguments: tuple[str, ...], *, cwd: Path) -> str:
    # A fixed argument vector and no shell: nothing the caller passes can
    # become part of the command being run.
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise EvidenceError(
            f"git {' '.join(arguments)} failed: {result.stderr.strip() or 'no output'}"
        )
    return result.stdout.strip()


def _attachment(argument: str) -> tuple[str, Path]:
    kind, separator, raw = argument.partition("=")
    if not separator or not raw:
        raise argparse.ArgumentTypeError(
            f"expected <kind>=<path>, received {argument!r}"
        )
    return kind, Path(raw)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-evidence",
        description=(
            "Write or verify the evidence manifest for one release gate. "
            "Derived facts come from configuration and git; everything else "
            "is a file with a hash."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    write = commands.add_parser("write", help="Write a gate manifest.")
    write.add_argument(
        "--gate", required=True, help="Gate name, e.g. M3b or resume-v1."
    )
    source = write.add_mutually_exclusive_group()
    source.add_argument("--config", type=Path, help="Optional TOML overlay.")
    source.add_argument(
        "--profile",
        choices=tuple(PROFILE_CONFIG_FILES),
        help="Validate and record a committed profile overlay.",
    )
    write.add_argument("--env-file", type=Path)
    write.add_argument("--secrets-dir", type=Path)
    write.add_argument(
        "--attach",
        action="append",
        type=_attachment,
        default=[],
        metavar="KIND=PATH",
        help=("Attach evidence. Repeatable. Kinds: " + ", ".join(EVIDENCE_KINDS) + "."),
    )
    write.add_argument(
        "--task-run-semantics-revision",
        help="The revision of a concrete Task snapshot this gate demonstrated.",
    )
    write.add_argument(
        "--known-limitation",
        action="append",
        default=[],
        metavar="TEXT",
        help="Repeatable. Recorded verbatim beside the evidence.",
    )
    write.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Record a manifest from an uncommitted tree, marked as such.",
    )
    write.add_argument(
        "--out",
        type=Path,
        help=f"Manifest path. Defaults to {EVIDENCE_ROOT}/<gate>/{MANIFEST_NAME}.",
    )

    verify = commands.add_parser(
        "verify", help="Re-hash the files a manifest points at."
    )
    verify.add_argument("manifest", type=Path)
    return parser


def run(argv: Sequence[str] | None = None) -> tuple[dict[str, Any], int]:
    """Execute one subcommand and return its payload and exit code."""

    args = _parser().parse_args(argv)
    if args.command == "verify":
        problems = verify_manifest(args.manifest)
        return (
            {
                "status": "ok" if not problems else "stale",
                "manifest": str(args.manifest),
                "problems": problems,
            },
            0 if not problems else 1,
        )

    root = repository_root()
    config_file = (
        PROFILE_CONFIG_FILES[args.profile] if args.profile is not None else args.config
    )
    settings = load_settings(
        config_file=config_file,
        env_file=args.env_file,
        secrets_dir=args.secrets_dir,
    )
    attachments = [attach(kind, path) for kind, path in args.attach]
    manifest = build_manifest(
        gate=args.gate,
        settings=settings,
        root=root,
        attachments=attachments,
        task_run_semantics_revision=args.task_run_semantics_revision,
        known_limitations=args.known_limitation,
        allow_dirty=args.allow_dirty,
    )
    out = args.out or root / EVIDENCE_ROOT / manifest["gate"] / MANIFEST_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "status": "written",
        "manifest": str(out),
        "missing": manifest["missing"],
    }, 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""

    try:
        payload, code = run(argv)
    except EvidenceError as error:
        print(json.dumps({"status": "refused", "reason": str(error)}, indent=2))
        return 2
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EVIDENCE_KINDS",
    "EVIDENCE_ROOT",
    "Attachment",
    "EvidenceError",
    "attach",
    "build_manifest",
    "config_facts",
    "digest",
    "git_state",
    "main",
    "repository_root",
    "run",
    "verify_manifest",
]
