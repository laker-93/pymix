"""add upload_attempt_table: the files one upload attempt asked to import

A Rekordbox or Serato import staged every audio file under `uploads/{user}`,
not the files the current attempt uploaded, and `uploads/` was only cleared after
a successful import. So anything an earlier attempt left behind went into beets
with the next one (laker-93/pymix#38). On prod on 2026-09-23 that meant 82 files
from an abandoned upload, which had never reached `/sync/map_meta` and so carried
no SUBBOX_ID, were imported into a user's library on an unrelated attempt.

The client names the attempt's files exactly once: the `/sync/map_meta` payload,
which it sends after uploading and before calling the import. This table keeps
what map_meta made of that payload, so the import can stage those files and
nothing else. There is one set per user, replaced by each map_meta and cleared
when the import that consumed it finishes. `attempt_id` names the set, so an
import clears the set it read and not one a newer map_meta recorded while it ran.

`relative_path` is the file under `uploads/{user}`, as map_meta found and tagged
it. `subbox_id` is the id map_meta tagged it with. The import checks the file
still carries it before staging.

Revision ID: 020
Revises: 019
Create Date: 2026-09-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '020'
down_revision: Union[str, None] = '019'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'upload_attempt_table',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.String, nullable=False),
        sa.Column('attempt_id', sa.String, nullable=False),
        sa.Column('relative_path', sa.String, nullable=False),
        sa.Column('subbox_id', sa.String, nullable=False),
        sa.Column('created_at', sa.Float, nullable=False),
    )
    op.create_index('ix_upload_attempt_table_user_id', 'upload_attempt_table', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_upload_attempt_table_user_id', table_name='upload_attempt_table')
    op.drop_table('upload_attempt_table')
