"""add job_table.detail

/sync/map_meta became a job (#237): it tags every uploaded file, which on a large
upload outlasted Cloudflare's 100s. When some files could not be tagged, the
synchronous endpoint answered 400 with which ones and why. The job keeps that
answer here, for GET /sync/map_meta/progress to hand back once it has finished.

Null on every other job, and on a map_meta job that tagged everything.

Revision ID: 027
Revises: 026
Create Date: 2026-09-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '027'
down_revision: Union[str, None] = '026'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('job_table', sa.Column('detail', sa.JSON, nullable=True))


def downgrade() -> None:
    op.drop_column('job_table', 'detail')
