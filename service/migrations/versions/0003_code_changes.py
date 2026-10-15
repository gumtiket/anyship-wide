"""Persist reviewed changes and real GitHub publication checkpoints."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "code_changes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False, unique=True
        ),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("base_sha", sa.String(64), nullable=False),
        sa.Column("base_tree", sa.String(64), nullable=False),
        sa.Column("branch", sa.String(255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("diff", sa.Text(), nullable=False),
        sa.Column("review_hash", sa.String(64), nullable=False),
        sa.Column("tree_sha", sa.String(64), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("pr_url", sa.Text(), nullable=False),
        sa.Column("pr_number", sa.BigInteger(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("lease_until", sa.BigInteger(), nullable=False),
    )


def downgrade():
    op.drop_table("code_changes")
