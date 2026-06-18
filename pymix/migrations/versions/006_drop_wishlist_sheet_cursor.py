"""drop wishlist_sheet_synced_through_row - dedup is now content-based, not row-position-based

Revision ID: 006
Revises: 005
Create Date: 2026-06-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '006'
down_revision: Union[str, None] = '005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('user_table', 'wishlist_sheet_synced_through_row')


def downgrade() -> None:
    op.add_column('user_table', sa.Column('wishlist_sheet_synced_through_row', sa.Integer, nullable=True))
