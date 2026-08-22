"""a project may be a directory

One nullable `root_path` column on `projects` (ADR-072). A project that has one
is a real directory on the machine the API runs on, and its coding sessions read
and write that tree; a project without one is exactly what it was before this
migration.

**This migration writes no data**, for the same reason 0030 wrote none. There is
no path that could be invented for an existing project: a root is a judgement
about which directory somebody is willing to hand an agent, and nothing in this
schema knows enough to make it. Guessing would produce projects that silently
point at a directory their owner never chose -- which is worse than the column
being NULL, because NULL is legible and a wrong path is not.

Nullable is therefore the normal state, not a migration artefact to be tidied
away later (ADR-072 §5.5, continuing ADR-071 §5.3).

The column is deliberately **not** unique. Two projects may point at one
directory: a person can have "the migration" and "the RAG evaluation" as two
pieces of work in one checkout, and they are two projects by every measure this
system has. Uniqueness would encode "a directory is a project", which is the
container model ADR-071 rejected and ADR-072 did not restore.

No CHECK constraint on the shape of the path either. The rules that make a path
safe are relative to a root that has been resolved on the machine holding it
(symlinks, `realpath`), and none of that is expressible in SQL. A constraint
here could only check spelling, which would read as a guarantee it does not
provide -- the guarantee lives in `domain/project_files.py` and
`adapters/filesystem/sandbox.py`, and it has to be the only place, or the next
reader will trust the weaker one.

Revision ID: 0031_project_root_path
Revises: 0030_projects
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_project_root_path"
down_revision: str | None = "0030_projects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Long enough for any real absolute path and short enough to stay a column.
#: `PATH_MAX` is 4096 on Linux and 1024 on macOS; this is the smaller of the two
#: plus room, because a root longer than the platform allows could never be
#: opened anyway and storing it would only defer the failure.
_ROOT_PATH_LENGTH = 2048


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("root_path", sa.String(_ROOT_PATH_LENGTH), nullable=True),
    )


def downgrade() -> None:
    # Dropping the column loses which directory each project pointed at, and
    # that is not recoverable from anything else in the schema. Recorded rather
    # than guarded against: a downgrade is a deliberate act, and the honest
    # thing is to say what it costs rather than to refuse it.
    op.drop_column("projects", "root_path")
