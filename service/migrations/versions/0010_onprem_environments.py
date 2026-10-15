"""On-premise registration, environment jobs and typed deployment targets."""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "onprem_environments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("env_id", sa.String(21), nullable=False),
        sa.Column("connection_kind", sa.String(24), nullable=False, server_default="ssh"),
        sa.Column("host", sa.String(255)),
        sa.Column("ssh_user", sa.String(32)),
        sa.Column("ssh_port", sa.Integer()),
        sa.Column("credential_ref", sa.String(255)),
        sa.Column("last_seen_at", sa.BigInteger()),
        sa.Column("public_ip", sa.String(45)),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=False),
        sa.Column("active_job_id", sa.String(36)),
        sa.Column("lease_until", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("deleted_at", sa.BigInteger()),
        sa.UniqueConstraint("env_id", name="uq_onprem_env_id"),
        sa.UniqueConstraint("workspace_id", "created_by", "request_id", name="uq_onprem_request"),
    )
    op.create_index("ix_onprem_environments_workspace_id", "onprem_environments", ["workspace_id"])
    op.create_table(
        "onprem_registration_tokens",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column(
            "environment_id", sa.String(36), sa.ForeignKey("onprem_environments.id"), nullable=False
        ),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("used_at", sa.BigInteger()),
    )
    op.create_index(
        "ix_onprem_registration_tokens_environment_id",
        "onprem_registration_tokens",
        ["environment_id"],
    )
    op.create_table(
        "onprem_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "environment_id", sa.String(36), sa.ForeignKey("onprem_environments.id"), nullable=False
        ),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("runtime_id", sa.String(36), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("logs_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("finished_at", sa.BigInteger()),
        sa.UniqueConstraint("environment_id", "request_id", name="uq_onprem_job_request"),
    )
    op.create_index("ix_onprem_jobs_environment_id", "onprem_jobs", ["environment_id"])
    with op.batch_alter_table("deployments") as batch:
        batch.alter_column("aws_environment_id", existing_type=sa.String(36), nullable=True)
        batch.add_column(sa.Column("onprem_environment_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_deployment_onprem", "onprem_environments", ["onprem_environment_id"], ["id"]
        )
        batch.create_index("ix_deployments_onprem_environment_id", ["onprem_environment_id"])
        batch.create_check_constraint(
            "ck_deployment_environment",
            "(aws_environment_id IS NOT NULL AND onprem_environment_id IS NULL) OR "
            "(aws_environment_id IS NULL AND onprem_environment_id IS NOT NULL)",
        )


def downgrade():
    # Refuse a downgrade that would discard on-premise deployment records.
    if (
        op.get_bind()
        .execute(
            sa.text("SELECT 1 FROM deployments WHERE onprem_environment_id IS NOT NULL LIMIT 1")
        )
        .first()
    ):
        raise RuntimeError("Remove on-premise deployment selections before downgrading.")
    with op.batch_alter_table("deployments") as batch:
        batch.drop_constraint("ck_deployment_environment", type_="check")
        batch.drop_constraint("fk_deployment_onprem", type_="foreignkey")
        batch.drop_index("ix_deployments_onprem_environment_id")
        batch.drop_column("onprem_environment_id")
        batch.alter_column("aws_environment_id", existing_type=sa.String(36), nullable=False)
    op.drop_table("onprem_jobs")
    op.drop_table("onprem_registration_tokens")
    op.drop_table("onprem_environments")
