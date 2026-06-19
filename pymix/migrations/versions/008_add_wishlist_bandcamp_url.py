"""add bandcamp_url to wishlist_table

Revision ID: 008
Revises: 007
Create Date: 2026-06-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '008'
down_revision: Union[str, None] = '007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('wishlist_table', sa.Column('bandcamp_url', sa.String, nullable=True))


def downgrade() -> None:
    op.drop_column('wishlist_table', 'bandcamp_url')
