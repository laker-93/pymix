"""add phase columns to job_table

An import job is several passes, not one (`beet import`, then the subbox_id map, then
the XML metadata), and the progress endpoint could only ever see the first of them —
its percentage came from beets' track count, so it read 100% for the entire tail
(laker-93/pymix#51). These three columns let the job say which pass it is in and how
far through that pass it is.

Nullable with no server_default: rows written before this migration, and export jobs
(which have no phases), simply have no phase — the progress endpoint treats a null
phase as the audio phase, which is what every pre-existing in-flight job is doing.

Revision ID: 016
Revises: 015
Create Date: 2026-08-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '016'
down_revision: Union[str, None] = '015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('job_table', sa.Column('phase', sa.String, nullable=True))
    op.add_column('job_table', sa.Column('phase_n_processed', sa.Integer, nullable=True))
    op.add_column('job_table', sa.Column('phase_n_total', sa.Integer, nullable=True))


def downgrade() -> None:
    op.drop_column('job_table', 'phase_n_total')
    op.drop_column('job_table', 'phase_n_processed')
    op.drop_column('job_table', 'phase')
