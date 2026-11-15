"""Add private question bookmarks.

Revision ID: c2a8d6e39104
Revises: af37b2c9401d
"""
from alembic import op
import sqlalchemy as sa

revision = 'c2a8d6e39104'
down_revision = 'af37b2c9401d'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('question_bookmarks',
        sa.Column('user_id', sa.String(32), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('question_id', sa.String(32), sa.ForeignKey('questions.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table('question_bookmarks')
