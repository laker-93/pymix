"""add a trash_batch_id column to job_table

A re-import that updates playlists in place (#203) keeps what it replaced in a
`playlist_entries` trash batch, so the import can be undone (#208). The client
learns of the batch from the job: /beets/import/progress returns this column
once the job has finished, and the import screen offers "3 playlists updated ·
Undo" from it.

Nullable with no default: every job before this one, and every job that replaced
no playlist, has no batch.

Revision ID: 023
Revises: 022
Create Date: 2026-09-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '023'
down_revision: Union[str, None] = '022'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('job_table', sa.Column('trash_batch_id', sa.String, nullable=True))


def downgrade() -> None:
    op.drop_column('job_table', 'trash_batch_id')
