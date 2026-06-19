"""make wishlist_table.artist and .title nullable

Revision ID: 009
Revises: 008
Create Date: 2026-06-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '009'
down_revision: Union[str, None] = '008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('wishlist_table', 'artist', existing_type=sa.String, nullable=True)
    op.alter_column('wishlist_table', 'title', existing_type=sa.String, nullable=True)


def downgrade() -> None:
    op.alter_column('wishlist_table', 'artist', existing_type=sa.String, nullable=False)
    op.alter_column('wishlist_table', 'title', existing_type=sa.String, nullable=False)
