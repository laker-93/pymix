"""drop playlist_path_table and playlist_node_table.migrated_from_name

Both existed for users without a playlist tree (#211, design-playlists-and-undo
§5.6, §6). `playlist_path_table` held a `none` user's joined playlist names split
back into their folders, for #205's migration to build their tree from, and
`migrated_from_name` held each migrated playlist's joined name, for its rollback.
Once every user is `live`, nothing reads either.

**Refuses to run while any user but `demo` is still `none`.** Such a user's folders
exist only in this table, and with it gone their migration can't rebuild them. The
upgrade raises, pymix doesn't start, and rolling back the image leaves everything as
it was. Migrate them first (`POST /admin/playlists/migrate` on the image before
this one), then deploy this again. `GET /admin/playlists/tree-state` shows who's left.

New users start `live` from here on, whatever creates the row.

The downgrade puts the table and column back empty: what they held is gone.

Revision ID: 025
Revises: 024
Create Date: 2026-09-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '025'
down_revision: Union[str, None] = '024'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# `demo` never has a tree (§4.3): its reads resolve to demoadmin and it can't import.
DEMO_USERNAME = 'demo'


def upgrade() -> None:
    still_none = sorted(u for (u,) in op.get_bind().execute(sa.text(
        "SELECT username FROM user_table WHERE playlist_tree_state <> 'live' AND username <> :demo"),
        {'demo': DEMO_USERNAME}))
    if still_none:
        raise RuntimeError(
            f"{len(still_none)} user(s) with no playlist tree yet: {', '.join(still_none)}. "
            "Their folders are only in playlist_path_table; migrate them before dropping it (pymix#211).")
    op.drop_table('playlist_path_table')
    op.drop_column('playlist_node_table', 'migrated_from_name')
    op.alter_column('user_table', 'playlist_tree_state', server_default='live')


def downgrade() -> None:
    op.alter_column('user_table', 'playlist_tree_state', server_default='none')
    op.add_column('playlist_node_table', sa.Column('migrated_from_name', sa.String, nullable=True))
    op.create_table(
        'playlist_path_table',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.String, nullable=False),
        sa.Column('display_name', sa.String, nullable=False),
        sa.Column('path_components', sa.JSON, nullable=False),
    )
