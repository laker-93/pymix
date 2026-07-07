"""collapse wishlist status imported->available and convert to enum

Revision ID: 010
Revises: 009
Create Date: 2026-06-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '010'
down_revision: Union[str, None] = '009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The wishlist states after collapsing the (always-equivalent) 'imported' into
# 'available'. Navidrome serves directly off the beets library, so "in beets" already
# means "playable now" — there was never a meaningful gap between the two.
WISHLIST_STATUSES = ('inbox', 'wishlist', 'downloaded', 'available', 'ignored')


def upgrade() -> None:
    # 1. Collapse the data first, so no row carries a value the new enum forbids.
    op.execute("UPDATE wishlist_table SET status = 'available' WHERE status = 'imported'")

    # 2. Create the native enum type and convert the column to it.
    wishlist_status = sa.Enum(*WISHLIST_STATUSES, name='wishlist_status')
    wishlist_status.create(op.get_bind(), checkfirst=True)
    op.alter_column(
        'wishlist_table',
        'status',
        type_=wishlist_status,
        postgresql_using='status::wishlist_status',
        existing_nullable=False,
    )


def downgrade() -> None:
    # Revert the column to a free String, then drop the enum type.
    op.alter_column(
        'wishlist_table',
        'status',
        type_=sa.String(),
        postgresql_using='status::text',
        existing_nullable=False,
    )
    sa.Enum(name='wishlist_status').drop(op.get_bind(), checkfirst=True)
