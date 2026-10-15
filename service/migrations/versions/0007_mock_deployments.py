"""Isolated development-only deployment selections and job history."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "mock_deployments",
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), primary_key=True),
        sa.Column("source", sa.String(36), nullable=False),
        sa.Column(
            "aws_environment_id", sa.String(36), sa.ForeignKey("aws_environments.id"), nullable=True
        ),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("adapter_env_id", sa.String(21), nullable=False, unique=True),
        sa.Column("set_name", sa.String(24), nullable=False),
        sa.Column("checked_runtime_id", sa.String(36), nullable=False),
        sa.Column("active_job_id", sa.String(36), nullable=True),
    )
    op.create_index(
        "ix_mock_deployments_aws_environment_id", "mock_deployments", ["aws_environment_id"]
    )
    op.create_table(
        "mock_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("runtime_id", sa.String(36), nullable=False),
        sa.Column("adapter_env_id", sa.String(21), nullable=False),
        sa.Column("target_label", sa.String(100), nullable=False),
        sa.Column("set_name", sa.String(24), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("scenario", sa.String(24), nullable=False),
        sa.Column("image_tag", sa.String(40), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("logs_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("finished_at", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("project_id", "request_id", name="uq_mock_job_request"),
    )
    op.create_index("ix_mock_jobs_project_id", "mock_jobs", ["project_id"])


def downgrade():
    op.drop_table("mock_jobs")
    op.drop_table("mock_deployments")
