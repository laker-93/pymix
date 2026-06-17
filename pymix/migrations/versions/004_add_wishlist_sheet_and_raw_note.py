"""add wishlist_sheet_id and raw_note

Revision ID: 004
Revises: 003
Create Date: 2026-06-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '004'
down_revision: Union[str, None] = '003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('user_table', sa.Column('wishlist_sheet_id', sa.String, nullable=True))
    op.add_column('wishlist_table', sa.Column('raw_note', sa.String, nullable=True))


def downgrade() -> None:
    op.drop_column('wishlist_table', 'raw_note')
    op.drop_column('user_table', 'wishlist_sheet_id')
