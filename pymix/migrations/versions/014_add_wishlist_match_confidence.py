"""add match_confidence to wishlist_table

Confidence [0,1] of the match that flipped a wishlist item to 'available', stamped by the
reconcile sweep alongside linked_subbox_id. Nullable — a flip to the terminal 'available'
state is otherwise unauditable, so recording the score lets a suspect flip be found after
the fact (query low/NULL-confidence 'available' rows) rather than only via a support
session (issue #42). Existing rows predate the column and keep NULL; no backfill.

Revision ID: 014
Revises: 013
Create Date: 2026-07-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '014'
down_revision: Union[str, None] = '013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'wishlist_table',
        sa.Column('match_confidence', sa.Float, nullable=True),
    )


def downgrade() -> None:
    op.drop_column('wishlist_table', 'match_confidence')
