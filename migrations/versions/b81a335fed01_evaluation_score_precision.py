"""Preserve fractional Gemini evaluation scores.

Revision ID: b81a335fed01
Revises: 7a23b9d04175
"""
from alembic import op
import sqlalchemy as sa

revision = "b81a335fed01"
down_revision = "7a23b9d04175"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("evaluations", "score", existing_type=sa.Integer(), type_=sa.Float(), existing_nullable=False)


def downgrade():
    raise RuntimeError("Downgrade could destroy fractional evaluation scores; use an explicit data migration.")
