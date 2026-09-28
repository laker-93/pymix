"""add user_table.playlist_names and playlist_node_table.navidrome_name

A playlist's Navidrome name becomes a projection of the tree (#229,
design-playlists-and-undo §18): its leaf, or for a `path` user its full path, so
third-party Subsonic clients see the folders. `playlist_names` says which, per user.
Everyone starts `leaf`, which is what they have now: this migration renames nothing.
POST /admin/playlists/names moves a user to `path`, and back.

`navidrome_name` is the name pymix last wrote or accepted for a playlist, so that an
edit made outside pymix (a mobile client, an old subbox-app) can be told from a
rename pymix still owes. A playlist node's `name` now holds its leaf too.

Neither is backfilled here: a migration can't read Navidrome. The first
reconciliation of each user fills a playlist node's `name` and `navidrome_name` from
the name Navidrome has, which is its leaf.

Revision ID: 026
Revises: 025
Create Date: 2026-09-28
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '026'
down_revision: Union[str, None] = '025'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('user_table', sa.Column('playlist_names', sa.String, nullable=False, server_default='leaf'))
    op.add_column('playlist_node_table', sa.Column('navidrome_name', sa.String, nullable=True))


def downgrade() -> None:
    """Refuses while any user is `path`: the image before this one would read their
    full-path names as leaves, and show `Bass / House` inside `Bass`. Move them back
    first (POST /admin/playlists/names with names 'leaf')."""
    still_path = sorted(u for (u,) in op.get_bind().execute(sa.text(
        "SELECT username FROM user_table WHERE playlist_names <> 'leaf'")))
    if still_path:
        raise RuntimeError(f"users still have path playlist names, move them back to leaf first: {still_path}")
    op.drop_column('playlist_node_table', 'navidrome_name')
    # Playlist nodes' names are left: the image before this one ignores them.
    op.drop_column('user_table', 'playlist_names')
