"""add soundcloud_url to wishlist_table

Revision ID: 011
Revises: 010
Create Date: 2026-07-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '011'
down_revision: Union[str, None] = '010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('wishlist_table', sa.Column('soundcloud_url', sa.String, nullable=True))


def downgrade() -> None:
    op.drop_column('wishlist_table', 'soundcloud_url')
