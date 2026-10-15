"""실제 배포: 환경의 공용 기반 값과 배포 대상, 작업 이력(모의 배포 테이블과 별개)."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

FOUNDATION_COLUMNS = (
    sa.Column("env_id", sa.String(21), nullable=True),
    sa.Column("host", sa.String(255), nullable=True),
    sa.Column("db_address", sa.String(255), nullable=True),
    sa.Column("db_port", sa.Integer(), nullable=True),
    sa.Column("db_secret_arn", sa.String(2048), nullable=True),
    sa.Column("state_bucket", sa.String(63), nullable=True),
)


def upgrade():
    # SQLite는 ALTER로 유일 제약을 못 붙이므로 batch로 한다(PostgreSQL에서는 ALTER로 처리된다).
    with op.batch_alter_table("aws_environments") as batch:
        for column in FOUNDATION_COLUMNS:
            batch.add_column(column)
        batch.create_unique_constraint("uq_aws_environments_env_id", ["env_id"])
    op.create_table(
        "deployments",
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), primary_key=True),
        sa.Column(
            "aws_environment_id",
            sa.String(36),
            sa.ForeignKey("aws_environments.id"),
            nullable=False,
        ),
        sa.Column("set_name", sa.String(24), nullable=False),
        sa.Column("app_name", sa.String(63), nullable=False),
        sa.Column("image_tag", sa.String(40), nullable=False),
        sa.Column("url", sa.String(512), nullable=False),
        sa.Column("active_job_id", sa.String(36), nullable=True),
        sa.Column("lease_until", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_deployments_aws_environment_id", "deployments", ["aws_environment_id"])
    op.create_table(
        "deploy_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("runtime_id", sa.String(36), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("set_name", sa.String(24), nullable=False),
        sa.Column("image_tag", sa.String(40), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("logs_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("finished_at", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("project_id", "request_id", name="uq_deploy_job_request"),
    )
    op.create_index("ix_deploy_jobs_project_id", "deploy_jobs", ["project_id"])


def downgrade():
    op.drop_table("deploy_jobs")
    op.drop_table("deployments")
    with op.batch_alter_table("aws_environments") as batch:
        batch.drop_constraint("uq_aws_environments_env_id", type_="unique")
        for column in FOUNDATION_COLUMNS:
            batch.drop_column(column.name)
