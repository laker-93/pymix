"""add a warnings column to job_table

`reason` only ever reaches the client on a *failed* job, which leaves no way to say
"this worked, but not on all of it". A Serato import is the case that needs it: the
user's crates routinely reference records they have never uploaded to subbox, so
some tracks legitimately cannot be matched and are left out of the playlists. That
is a successful import with a caveat, not a failure — and reporting it as an
unqualified success is the trap this codebase has already been bitten by, where a
job returned result=true while every metadata write had silently failed.

Separate from `reason` rather than overloading it, so the client can render one as
an error and the other as a notice without having to guess which it got.

Nullable with no server_default: jobs written before this migration have no recorded
warnings, which is exactly the truth about them.

Revision ID: 018
Revises: 017
Create Date: 2026-08-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '018'
down_revision: Union[str, None] = '017'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('job_table', sa.Column('warnings', sa.String, nullable=True))


def downgrade() -> None:
    op.drop_column('job_table', 'warnings')
