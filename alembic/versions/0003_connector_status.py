"""add connector_status table

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'connector_status',
        sa.Column('connector_name', sa.String(length=50), nullable=False),
        sa.Column('schema_version', sa.Integer(), nullable=False),
        sa.Column('scheduler_heartbeat_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('brands_total', sa.Integer(), nullable=False),
        sa.Column('brands_active', sa.Integer(), nullable=False),
        sa.Column('brand_match_summary', sa.JSON(), nullable=True),
        sa.Column('last_incremental_sync_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('catalogs', sa.JSON(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('connector_name'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('connector_status')
