"""Shared per-client fixed-window rate limits.

Revision ID: d67f2c18ce91
Revises: 452be14f5c90
"""
from alembic import op
import sqlalchemy as sa

revision = 'd67f2c18ce91'
down_revision = '452be14f5c90'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('request_limits',
        sa.Column('client_hash', sa.String(64), nullable=False),
        sa.Column('category', sa.String(32), nullable=False),
        sa.Column('bucket', sa.BigInteger(), nullable=False),
        sa.Column('count', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('client_hash', 'category', 'bucket'))
    op.create_index('ix_request_limits_bucket', 'request_limits', ['bucket'])


def downgrade():
    op.drop_table('request_limits')
