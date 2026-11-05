"""Add self-service accounts and per-account daily interview usage.

Revision ID: 9d10cbe4a8f2
Revises: d67f2c18ce91
"""
from alembic import op
import sqlalchemy as sa


revision = "9d10cbe4a8f2"
down_revision = "d67f2c18ce91"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("email", sa.String(length=320), nullable=True))
    op.add_column("users", sa.Column("password_hash", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("is_admin", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("users", sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False))
    op.add_column("users", sa.Column("daily_interview_limit", sa.Integer(), server_default="2", nullable=False))
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    with op.batch_alter_table("users") as batch_op:
        batch_op.create_check_constraint(
            "ck_users_daily_interview_limit_nonnegative", "daily_interview_limit >= 0"
        )
    op.create_table(
        "daily_interview_usage",
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("interview_count", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("interview_count >= 0", name="ck_daily_interview_usage_interview_count_nonnegative"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "usage_date"),
    )


def downgrade():
    op.drop_table("daily_interview_usage")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("ck_users_daily_interview_limit_nonnegative", type_="check")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_column("users", "daily_interview_limit")
    op.drop_column("users", "is_active")
    op.drop_column("users", "is_admin")
    op.drop_column("users", "password_hash")
    op.drop_column("users", "email")
