"""Keep submitted role ARNs independently of connection verification."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "aws_environments", sa.Column("submitted_role_arn", sa.String(2048), nullable=True)
    )
    environments = sa.table(
        "aws_environments", sa.column("submitted_role_arn"), sa.column("role_arn")
    )
    op.execute(environments.update().values(submitted_role_arn=environments.c.role_arn))


def downgrade():
    op.drop_column("aws_environments", "submitted_role_arn")
