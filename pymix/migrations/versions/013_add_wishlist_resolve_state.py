"""add resolve_state to wishlist_table

Work-state for the async resolve loop: 'pending' (raw artist/title or an unparsed URL
still to be refined against MusicBrainz / yt-dlp), 'resolved' (a confident match applied,
or arrived pre-resolved), or 'nomatch' (resolution ran, no confident hit — terminal). New
items start 'pending'; the loop selects on (metadata_source='auto', resolve_state='pending').

Revision ID: 013
Revises: 012
Create Date: 2026-07-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '013'
down_revision: Union[str, None] = '012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'wishlist_table',
        sa.Column('resolve_state', sa.String, nullable=False, server_default='pending'),
    )
    # Existing rows predate async resolution. Treat the whole backlog as already settled
    # ('resolved') rather than 'pending', so the loop doesn't re-query MusicBrainz for
    # every historical item and risk rewriting a title the user was already happy with.
    # Only items created after this migration start 'pending'.
    op.execute("UPDATE wishlist_table SET resolve_state = 'resolved'")
    # The resolve loop selects auto-provenance, still-pending items; index the pair so the
    # per-user sweep's filter stays cheap as the table grows.
    op.create_index(
        'ix_wishlist_metadata_source_resolve_state',
        'wishlist_table',
        ['metadata_source', 'resolve_state'],
    )


def downgrade() -> None:
    op.drop_index('ix_wishlist_metadata_source_resolve_state', table_name='wishlist_table')
    op.drop_column('wishlist_table', 'resolve_state')
