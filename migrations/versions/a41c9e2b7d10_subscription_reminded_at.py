"""subscription reminded_at

Revision ID: a41c9e2b7d10
Revises: 6d87c351a3c8
Create Date: 2026-11-17 14:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a41c9e2b7d10'
down_revision: Union[str, Sequence[str], None] = '6d87c351a3c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('reminded_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.drop_column('reminded_at')
