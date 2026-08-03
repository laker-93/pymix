"""add invite_request_table

Beta-invite requests captured by the unauthenticated `POST /invite-request` (the demo →
beta-tester funnel, subbox-app#69). No user_id: the requester has no account yet, which
is the whole point. `email` is unique so a re-submission upserts rather than duplicating,
and `status` lets a fulfilled request be marked off — fulfilment is manual (read the
table, mint a UserTokenRow), so there is no endpoint that writes it.

Revision ID: 015
Revises: 014
Create Date: 2026-08-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '015'
down_revision: Union[str, None] = '014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'invite_request_table',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('email', sa.String, unique=True, nullable=False),
        sa.Column('dj_software', sa.String, nullable=False),
        sa.Column('dj_software_other', sa.String),
        sa.Column('status', sa.String, nullable=False, server_default='new'),
        sa.Column('created_at', sa.Float),
        sa.Column('updated_at', sa.Float),
    )
    op.create_index('ix_invite_request_table_status', 'invite_request_table', ['status'])


def downgrade() -> None:
    op.drop_index('ix_invite_request_table_status', table_name='invite_request_table')
    op.drop_table('invite_request_table')
