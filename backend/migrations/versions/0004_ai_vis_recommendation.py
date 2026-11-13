"""AI visibility: first-named brand, recommendation intent, schema version

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('ai_vis_import', sa.Column('schema_version', sa.Integer(), nullable=False, server_default='1'))
    op.add_column('ai_vis_row', sa.Column('first_brand', sa.String(length=30), nullable=True))
    op.add_column('ai_vis_row', sa.Column('recommendation_intent', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column('ai_vis_row', 'recommendation_intent')
    op.drop_column('ai_vis_row', 'first_brand')
    op.drop_column('ai_vis_import', 'schema_version')
