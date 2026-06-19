"""add wishlist_table

Revision ID: 003
Revises: 002
Create Date: 2026-06-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '003'
down_revision: Union[str, None] = '002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'wishlist_table',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('wishlist_id', sa.String, unique=True, nullable=False),
        sa.Column('user_id', sa.String, nullable=False),
        sa.Column('artist', sa.String, nullable=False),
        sa.Column('title', sa.String, nullable=False),
        sa.Column('album', sa.String),
        sa.Column('status', sa.String, nullable=False),
        sa.Column('youtube_video_id', sa.String),
        sa.Column('youtube_url', sa.String),
        sa.Column('linked_subbox_id', sa.String),
        sa.Column('created_at', sa.Float),
        sa.Column('updated_at', sa.Float),
    )
    op.create_index('ix_wishlist_table_user_id', 'wishlist_table', ['user_id'])
    op.create_index('ix_wishlist_table_status', 'wishlist_table', ['status'])
    op.create_index('ix_wishlist_table_artist', 'wishlist_table', ['artist'])
    op.create_index('ix_wishlist_table_title', 'wishlist_table', ['title'])


def downgrade() -> None:
    op.drop_index('ix_wishlist_table_title', table_name='wishlist_table')
    op.drop_index('ix_wishlist_table_artist', table_name='wishlist_table')
    op.drop_index('ix_wishlist_table_status', table_name='wishlist_table')
    op.drop_index('ix_wishlist_table_user_id', table_name='wishlist_table')
    op.drop_table('wishlist_table')
