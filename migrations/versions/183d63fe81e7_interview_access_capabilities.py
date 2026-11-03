"""Add hashed interview access capabilities.

Revision ID: 183d63fe81e7
Revises: c65c0a01f4b8
"""
from alembic import op
import sqlalchemy as sa

revision = "183d63fe81e7"
down_revision = "c65c0a01f4b8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "interview_access",
        sa.Column("interview_id", sa.String(32), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["interview_id"], ["interviews.id"], ondelete="CASCADE",
                                name=op.f("fk_interview_access_interview_id_interviews")),
        sa.PrimaryKeyConstraint("interview_id", name=op.f("pk_interview_access")),
        sa.UniqueConstraint("token_digest", name=op.f("uq_interview_access_token_digest")),
    )


def downgrade():
    op.drop_table("interview_access")
