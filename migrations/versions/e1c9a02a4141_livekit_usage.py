"""Track restricted LiveKit interview admission and expiry.

Revision ID: e1c9a02a4141
Revises: d67f2c18ce91
"""
from alembic import op
import sqlalchemy as sa

revision = 'e1c9a02a4141'
down_revision = 'd67f2c18ce91'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('interview_usage',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('interview_id', sa.String(32), sa.ForeignKey('interviews.id', ondelete='CASCADE'), nullable=False),
        sa.Column('account_name', sa.String(128), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ended_at', sa.DateTime(timezone=True)))
    op.create_index('ix_interview_usage_interview_id', 'interview_usage', ['interview_id'])
    op.create_index('ix_interview_usage_account_name', 'interview_usage', ['account_name'])
    op.create_index('uq_interview_usage_active_account', 'interview_usage', ['account_name'], unique=True,
                    postgresql_where=sa.text('ended_at IS NULL'))


def downgrade():
    op.drop_table('interview_usage')
