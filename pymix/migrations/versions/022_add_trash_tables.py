"""add the trash tables: a delete moves things aside instead of destroying them

A track delete unlinked the file and removed its pymix rows, so a mistaken delete
could not be undone, even by support (#200). Now it moves the file into
`/private-music/_trash/{user}/{batch_id}/` and records what it moved here, so the
batch can be restored until the reaper purges it.

`trash_batch_table` is one undoable delete; `trash_item_table` is each thing it
holds, with the snapshot its restore needs. A batch's state is computed from its
items, never stored. `trash_reaper_run_table` is the reaper reporting itself.

Numbered 022 although the design reserved 022 for the playlist tree (#201): this
landed first.

Revision ID: 022
Revises: 021
Create Date: 2026-09-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '022'
down_revision: Union[str, None] = '021'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'trash_batch_table',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('batch_id', sa.String, nullable=False, unique=True),
        sa.Column('user_id', sa.String, nullable=False),
        sa.Column('kind', sa.String, nullable=False),
        sa.Column('label', sa.String, nullable=False),
        sa.Column('bytes', sa.BigInteger, nullable=False, server_default='0'),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('expires_at', sa.Float, nullable=False),
    )
    op.create_index('ix_trash_batch_table_user_id', 'trash_batch_table', ['user_id'])
    op.create_index('ix_trash_batch_table_expires_at', 'trash_batch_table', ['expires_at'])

    op.create_table(
        'trash_item_table',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('batch_id', sa.String, nullable=False),
        sa.Column('user_id', sa.String, nullable=False),
        sa.Column('kind', sa.String, nullable=False),
        sa.Column('state', sa.String, nullable=False),
        sa.Column('subbox_id', sa.String, nullable=True),
        sa.Column('relative_path', sa.String, nullable=True),
        sa.Column('size', sa.BigInteger, nullable=True),
        sa.Column('sha256', sa.String, nullable=True),
        sa.Column('media_file_id', sa.String, nullable=True),
        sa.Column('snapshot', sa.JSON, nullable=True),
        sa.Column('error', sa.String, nullable=True),
        sa.Column('updated_at', sa.Float, nullable=False),
    )
    op.create_index('ix_trash_item_table_batch_id', 'trash_item_table', ['batch_id'])

    op.create_table(
        'trash_reaper_run_table',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('started_at', sa.Float, nullable=False),
        sa.Column('finished_at', sa.Float, nullable=True),
        sa.Column('n_batches_purged', sa.Integer, nullable=False, server_default='0'),
        sa.Column('n_missing_swept', sa.Integer, nullable=False, server_default='0'),
        sa.Column('n_failures', sa.Integer, nullable=False, server_default='0'),
        sa.Column('errors', sa.String, nullable=True),
    )


def downgrade() -> None:
    op.drop_table('trash_reaper_run_table')
    op.drop_index('ix_trash_item_table_batch_id', table_name='trash_item_table')
    op.drop_table('trash_item_table')
    op.drop_index('ix_trash_batch_table_expires_at', table_name='trash_batch_table')
    op.drop_index('ix_trash_batch_table_user_id', table_name='trash_batch_table')
    op.drop_table('trash_batch_table')
