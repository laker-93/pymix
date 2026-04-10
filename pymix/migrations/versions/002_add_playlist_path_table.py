"""add playlist_path_table

Revision ID: 002
Revises: 001
Create Date: 2026-04-07
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '002'
down_revision: Union[str, None] = '001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'playlist_path_table',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.String, nullable=False),
        sa.Column('display_name', sa.String, nullable=False),
        sa.Column('path_components', sa.JSON, nullable=False),
    )


def downgrade() -> None:
    op.drop_table('playlist_path_table')
