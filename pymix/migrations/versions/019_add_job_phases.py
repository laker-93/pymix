"""add a phases column to job_table

`reason` and `warnings` are prose, and prose can only carry one phase's worth of a
job that has several. A Rekordbox re-import of an already-uploaded library applies
BPM, ratings and cues to every track and finishes with `0 tracks uploaded, 0 tracks
imported` — both true, both counting newly-landed audio, and together they tell the
user their re-import did nothing when it in fact rewrote their whole library
(laker-93/subbox-app#50).

So the per-phase counts the ledger already keeps (#171, migration 018's sibling) get
somewhere to live: a list of {phase, total, ok, skipped, failed}, one entry per phase
the job actually recorded. The client then has the number it was missing, and adding
a phase later costs a server change rather than an edit to every screen.

JSON rather than columns because the phases a job runs are not fixed — a
metadata-only import never enters `mapping_ids` at all — and because nothing queries
on them: this column is read whole, by the job it belongs to.

Nullable with no server_default, like `reason` and `warnings` before it: a job
written before this migration recorded no phases, and an empty list would be a claim
that it ran none.

Revision ID: 019
Revises: 018
Create Date: 2026-09-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '019'
down_revision: Union[str, None] = '018'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('job_table', sa.Column('phases', sa.JSON, nullable=True))


def downgrade() -> None:
    op.drop_column('job_table', 'phases')
