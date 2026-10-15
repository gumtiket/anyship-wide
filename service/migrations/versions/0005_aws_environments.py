"""Persist AWS onboarding requests and verified role connections."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "aws_environments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("region", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(64), nullable=False, unique=True),
        sa.Column("template_url", sa.Text(), nullable=False),
        sa.Column("service_role_arn", sa.String(2048), nullable=False),
        sa.Column("stack_name", sa.String(128), nullable=False),
        sa.Column("role_name", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("role_arn", sa.String(2048), nullable=True),
        sa.Column("aws_account_id", sa.String(12), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("verified_at", sa.BigInteger(), nullable=True),
        sa.Column("verification_token", sa.String(36), nullable=False),
        sa.Column("lease_until", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "workspace_id", "created_by", "request_id", name="uq_aws_environment_request"
        ),
        sa.UniqueConstraint("workspace_id", "role_arn", name="uq_aws_environment_role"),
    )
    op.create_index("ix_aws_environments_workspace_id", "aws_environments", ["workspace_id"])


def downgrade():
    op.drop_index("ix_aws_environments_workspace_id", table_name="aws_environments")
    op.drop_table("aws_environments")
