"""Preserve each user's repository onboarding across GitHub redirects."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "repository_connections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("repository_url", sa.String(2048), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("install_state_hash", sa.String(64), nullable=False),
        sa.Column("install_expires_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("user_id", "workspace_id", name="uq_connection_user_workspace"),
    )


def downgrade():
    op.drop_table("repository_connections")
