"""add playlist_node_table and user_table.playlist_tree_state

A playlist node gives a playlist or folder an identity pymix owns (#201,
design-playlists-and-undo §1, §4.1), as `subbox_id` does a track. It carries the
nesting (`parent_id`, `position`) that joined names like "House / Deep" encode
today, where the playlist came from in Rekordbox or Serato (`source_path`), and,
from #207, whether it is in the trash.

`playlist_tree_state` says whether a user has a tree at all (§4.4). Every user
starts 'none', and nothing writes nodes for a 'none' user, so this migration
changes nothing anyone sees. Only #205's migration pass moves users to 'live'.

A playlist's name is not stored: it lives in Navidrome only.

Revision ID: 024
Revises: 023
Create Date: 2026-09-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '024'
down_revision: Union[str, None] = '023'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'user_table',
        sa.Column('playlist_tree_state', sa.String, nullable=False, server_default='none'),
    )
    op.create_table(
        'playlist_node_table',
        sa.Column('node_id', sa.String, primary_key=True),
        sa.Column('user_id', sa.String, nullable=False),
        sa.Column('parent_id', sa.String, sa.ForeignKey('playlist_node_table.node_id'), nullable=True),
        sa.Column('position', sa.Integer, nullable=False),
        sa.Column('kind', sa.String, nullable=False),
        sa.Column('name', sa.String, nullable=True),
        sa.Column('navidrome_playlist_id', sa.String, nullable=True),
        sa.Column('source_path', sa.JSON, nullable=True),
        sa.Column('origin', sa.String, nullable=False),
        sa.Column('migrated_from_name', sa.String, nullable=True),
        sa.Column('trash_batch_id', sa.String, sa.ForeignKey('trash_batch_table.batch_id'), nullable=True),
        sa.Column('created_at', sa.Float, nullable=False),
        sa.Column('updated_at', sa.Float, nullable=False),
        # NULLs are distinct, so any number of folders share (user, NULL).
        sa.UniqueConstraint('user_id', 'navidrome_playlist_id'),
    )
    op.create_index('ix_playlist_node_table_user_id', 'playlist_node_table', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_playlist_node_table_user_id', table_name='playlist_node_table')
    op.drop_table('playlist_node_table')
    op.drop_column('user_table', 'playlist_tree_state')
