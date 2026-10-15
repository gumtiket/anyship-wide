"""환경 이전 뒤 원래 환경에 멈춘 채 남은 앱의 기록(`previous_*`). 지우기 전까지 서비스가 그 앱을 잊지 않게 한다."""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("previous_kind", 8),
    ("previous_environment_id", 36),
    ("previous_app_name", 63),
    ("previous_url", 512),
)


def upgrade():
    with op.batch_alter_table("deployments") as batch:
        for name, length in _COLUMNS:
            batch.add_column(sa.Column(name, sa.String(length), nullable=False, server_default=""))


def downgrade():
    # 원래 환경에 멈춘 앱이 남아 있는 기록을 버리면 그 앱을 서비스가 더는 지울 수 없다.
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM deployments WHERE previous_app_name <> '' LIMIT 1"))
        .first()
    ):
        raise RuntimeError(
            "Remove the stopped apps left on previous environments before downgrading."
        )
    with op.batch_alter_table("deployments") as batch:
        for name, _ in reversed(_COLUMNS):
            batch.drop_column(name)
