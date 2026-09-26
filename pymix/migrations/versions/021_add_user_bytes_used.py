"""add a bytes_used column to user_table

The storage quota was checked by walking `/private-music/<user>` and stat()ing every
file, on every upload, every import, and every watch-dir filesystem event (#183). It
re-derived a figure pymix already had in hand: every path that changes a library
knows how many bytes it just moved in or deleted.

So the library's size is kept here instead, moved by the imports and deletes that
change it and corrected by a periodic walk (see DbController.reconcile_library_bytes).

Nullable with no server_default: NULL means "never measured", which is every
existing user when this runs. The first quota check for such a user walks the
library once and stores the result, so there is no backfill step to forget, and a
0 is never mistaken for an empty library.

Staging is deliberately not stored. It is measured live on each check: it only ever
holds one import's worth of audio plus whatever a failed import left behind, and
that residue is exactly what the quota used to miss.

Revision ID: 021
Revises: 020
Create Date: 2026-09-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '021'
down_revision: Union[str, None] = '020'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('user_table', sa.Column('bytes_used', sa.BigInteger, nullable=True))


def downgrade() -> None:
    op.drop_column('user_table', 'bytes_used')
