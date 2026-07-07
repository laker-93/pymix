"""add metadata_source to wishlist_table

Tracks how an item's artist/title were arrived at: 'auto' (extracted by pymix from a
link / string / MusicBrainz) or 'user' (edited by the user in the client). Automatic
re-matching must never overwrite a 'user' item's artist/title.

Revision ID: 012
Revises: 011
Create Date: 2026-07-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '012'
down_revision: Union[str, None] = '011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default backfills every existing row as 'auto' (they predate any provenance
    # tracking, so none were user-confirmed).
    op.add_column(
        'wishlist_table',
        sa.Column('metadata_source', sa.String, nullable=False, server_default='auto'),
    )


def downgrade() -> None:
    op.drop_column('wishlist_table', 'metadata_source')
