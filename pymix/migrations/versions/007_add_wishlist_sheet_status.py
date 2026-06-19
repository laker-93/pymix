"""add wishlist_sheet_status and wishlist_sheet_error

Revision ID: 007
Revises: 006
Create Date: 2026-06-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '007'
down_revision: Union[str, None] = '006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('user_table', sa.Column('wishlist_sheet_status', sa.String, nullable=True))
    op.add_column('user_table', sa.Column('wishlist_sheet_error', sa.String, nullable=True))


def downgrade() -> None:
    op.drop_column('user_table', 'wishlist_sheet_error')
    op.drop_column('user_table', 'wishlist_sheet_status')
