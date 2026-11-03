"""Shared Flask sessions.

Revision ID: 452be14f5c90
Revises: b81a335fed01
"""
from alembic import op
import sqlalchemy as sa

revision = '452be14f5c90'
down_revision = 'b81a335fed01'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('web_sessions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('session_id', sa.String(255), unique=True),
        sa.Column('data', sa.LargeBinary()),
        sa.Column('expiry', sa.DateTime()))


def downgrade():
    raise RuntimeError('Dropping session data requires an explicit migration.')
