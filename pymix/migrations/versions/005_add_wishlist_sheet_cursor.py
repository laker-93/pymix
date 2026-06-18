"""add wishlist_sheet_synced_through_row

Revision ID: 005
Revises: 004
Create Date: 2026-06-17
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '005'
down_revision: Union[str, None] = '004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('user_table', sa.Column('wishlist_sheet_synced_through_row', sa.Integer, nullable=True))


def downgrade() -> None:
    op.drop_column('user_table', 'wishlist_sheet_synced_through_row')
