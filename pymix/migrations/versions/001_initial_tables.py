"""initial tables

Revision ID: 001
Revises:
Create Date: 2026-03-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_table',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('username', sa.String, unique=True, nullable=False),
        sa.Column('password', sa.String, nullable=False),
        sa.Column('email', sa.String, nullable=False),
        sa.Column('user_id', sa.String, unique=True, nullable=False),
        sa.Column('beets_port', sa.Integer, nullable=False),
        sa.Column('subsonic_port', sa.Integer, nullable=False),
        sa.Column('max_library_size', sa.BigInteger, nullable=False),
    )

    op.create_table(
        'session_table',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('session_id', sa.String, unique=True, nullable=False),
        sa.Column('user_id', sa.String, nullable=False),
    )

    op.create_table(
        'subbox_beets_map_table',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.String, nullable=False),
        sa.Column('subbox_id', sa.String, nullable=False),
        sa.Column('beet_id', sa.Integer, nullable=False),
        sa.Column('created_at', sa.String),
    )

    op.create_table(
        'library_table',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.String, nullable=False),
        sa.Column('subbox_id', sa.String, nullable=False),
        sa.Column('cuedata', sa.JSON),
        sa.Column('source_app', sa.String),
        sa.Column('updated_at', sa.Float),
        sa.Column('version', sa.Integer, server_default='1'),
    )

    op.create_table(
        'meta_history_table',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.String, nullable=False),
        sa.Column('subbox_id', sa.String, nullable=False),
        sa.Column('version', sa.Integer),
        sa.Column('hash', sa.String),
        sa.Column('cuedata', sa.JSON),
        sa.Column('source_app', sa.String),
        sa.Column('change_type', sa.String),
        sa.Column('changed_at', sa.Float),
    )

    op.create_table(
        'user_job_table',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.String, nullable=False),
        sa.Column('job_id', sa.String, nullable=False),
    )

    op.create_table(
        'job_table',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('job_id', sa.String, unique=True, nullable=False),
        sa.Column('name', sa.String),
        sa.Column('n_tracks_to_import', sa.Integer, nullable=True),
        sa.Column('total_n_imported_tracks', sa.Integer, nullable=True),
        sa.Column('total_n_tracks_to_export', sa.Integer, nullable=True),
        sa.Column('n_exported_tracks', sa.Integer, nullable=True),
        sa.Column('in_progress', sa.Boolean, server_default='true'),
        sa.Column('result', sa.Boolean, nullable=True),
    )

    op.create_table(
        'original_track_meta_map_table',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.String, nullable=False),
        sa.Column('subbox_id', sa.String, nullable=False),
        sa.Column('user_location', sa.String),
        sa.Column('staging_location', sa.String),
        sa.Column('original_name', sa.String),
        sa.Column('original_artist', sa.String),
        sa.Column('original_album', sa.String),
    )

    op.create_table(
        'user_token_table',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.String, server_default=''),
        sa.Column('token', sa.String, nullable=False),
    )


def downgrade() -> None:
    op.drop_table('user_token_table')
    op.drop_table('original_track_meta_map_table')
    op.drop_table('job_table')
    op.drop_table('user_job_table')
    op.drop_table('meta_history_table')
    op.drop_table('library_table')
    op.drop_table('subbox_beets_map_table')
    op.drop_table('session_table')
    op.drop_table('user_table')
