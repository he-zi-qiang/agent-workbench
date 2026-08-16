"""a success may carry its caveat on the row

ADR-060 made `mark_succeeded(detail=...)` a real call: a Task whose reviewer
ran out of revisions still disputing the draft settles succeeded with the
dispute recorded in `status_detail`, which is where the console reads it. The
0011 check constraint predates that -- it required `status_detail IS NULL` for
every succeeded row, so the first caveated settlement violated
`ck_task_runs_task_runs_status_detail` (CI's fresh-migration database caught
what a longer-lived local one did not).

`succeeded` moves to its own arm with the detail *optional*, not required:
most successes have nothing to confess, and forcing a sentence onto them
would manufacture noise for the one reader the field exists to serve. The
detail-required statuses and the detail-forbidden in-flight statuses keep
exactly the rule they had.

The downgrade nulls succeeded rows' details before re-tightening, because a
constraint cannot be restored over rows that violate it. That loses recorded
caveats -- acceptable for a downgrade, and the review itself is still in the
thread's checkpoint (`review_result`), so nothing is lost that cannot be
re-derived.

Revision ID: 0029_succeeded_carries_caveat
Revises: 0028_session_last_activity
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0029_succeeded_carries_caveat"
down_revision: str | None = "0028_session_last_activity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "ck_task_runs_task_runs_status_detail"

_OLD = (
    "(status IN ('waiting_migration', 'failed', 'cancelled', 'dead_letter') "
    "AND status_detail IS NOT NULL) OR "
    "(status IN ('queued', 'running', 'waiting_approval', 'succeeded') "
    "AND status_detail IS NULL)"
)

_NEW = (
    "(status IN ('waiting_migration', 'failed', 'cancelled', 'dead_letter') "
    "AND status_detail IS NOT NULL) OR "
    "(status IN ('queued', 'running', 'waiting_approval') "
    "AND status_detail IS NULL) OR "
    "(status = 'succeeded')"
)


def upgrade() -> None:
    op.drop_constraint(op.f(_NAME), "task_runs", type_="check")
    op.create_check_constraint(op.f(_NAME), "task_runs", _NEW)


def downgrade() -> None:
    op.execute("UPDATE task_runs SET status_detail = NULL WHERE status = 'succeeded'")
    op.drop_constraint(op.f(_NAME), "task_runs", type_="check")
    op.create_check_constraint(op.f(_NAME), "task_runs", _OLD)
