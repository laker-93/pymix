"""add a failure reason column to job_table

A failed import job had nowhere to record *why* it failed. `run_import_task` caught the
exception, logged it, and marked the job `result=False`; `/beets/import/progress` then
returned `reason: ""` for it, because the only branch that ever set a reason was the
"no in-progress jobs found" one. The client faithfully renders `reason` and so always
fell through to its generic "Import failed" string — the actual cause existed only in
the pymix container logs (laker-93/subbox-app#48).

Nullable with no server_default: jobs written before this migration have no recorded
reason, which is exactly the truth about them.

Revision ID: 017
Revises: 016
Create Date: 2026-08-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '017'
down_revision: Union[str, None] = '016'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('job_table', sa.Column('reason', sa.String, nullable=True))


def downgrade() -> None:
    op.drop_column('job_table', 'reason')
