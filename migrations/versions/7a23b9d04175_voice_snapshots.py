"""Store voice orchestration state under an interview row lock.

Revision ID: 7a23b9d04175
Revises: 183d63fe81e7
"""
from alembic import op
import sqlalchemy as sa

revision = "7a23b9d04175"
down_revision = "183d63fe81e7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("voice_snapshots",
        sa.Column("interview_id", sa.String(32), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["interview_id"], ["interviews.id"], ondelete="CASCADE", name=op.f("fk_voice_snapshots_interview_id_interviews")),
        sa.PrimaryKeyConstraint("interview_id", name=op.f("pk_voice_snapshots")))


def downgrade():
    op.drop_table("voice_snapshots")
