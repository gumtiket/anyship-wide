"""AI analysis history and project leases, preserving placeholder changes."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "analysis_slots",
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), primary_key=True),
        sa.Column("active_id", sa.String(36), nullable=True),
        sa.Column("lease_until", sa.BigInteger(), nullable=False),
    )
    columns = [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
    ]
    for name, length in (
        ("request_id", 36),
        ("runtime_id", 36),
        ("repository", 512),
        ("base_branch", 255),
        ("base_sha", 40),
        ("base_tree", 40),
        ("provider", 16),
        ("target_env", 16),
        ("status", 16),
        ("review_hash", 64),
        ("publish_status", 16),
        ("branch", 255),
        ("tree_sha", 40),
        ("commit_sha", 40),
        ("error_code", 64),
    ):
        columns.append(sa.Column(name, sa.String(length), nullable=False))
    for name in ("logs_json", "report_json", "files_json", "diff", "pr_url", "error"):
        columns.append(sa.Column(name, sa.Text(), nullable=False))
    for name in ("repository_id", "pr_number", "lease_until", "created_at"):
        columns.append(sa.Column(name, sa.BigInteger(), nullable=False))
    columns.append(sa.Column("finished_at", sa.BigInteger(), nullable=True))
    op.create_table(
        "analysis_runs",
        *columns,
        sa.UniqueConstraint("project_id", "request_id", name="uq_analysis_request"),
    )
    op.create_index("ix_analysis_runs_project_id", "analysis_runs", ["project_id"])


def downgrade():
    op.drop_table("analysis_runs")
    op.drop_table("analysis_slots")
