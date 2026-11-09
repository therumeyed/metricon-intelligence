"""AI search visibility import tables

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

BIGPK = sa.BigInteger().with_variant(sa.Integer(), 'sqlite')


def upgrade() -> None:
    op.create_table('ai_vis_import',
    sa.Column('id', BIGPK, autoincrement=True, nullable=False),
    sa.Column('file_name', sa.String(length=300), nullable=False),
    sa.Column('file_sha256', sa.String(length=64), nullable=False),
    sa.Column('source_note', sa.Text(), nullable=True),
    sa.Column('period_start', sa.Date(), nullable=True),
    sa.Column('period_end', sa.Date(), nullable=True),
    sa.Column('imported_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('row_count', sa.Integer(), nullable=False, server_default='0'),
    sa.Column('is_current', sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column('min_rows', sa.Integer(), nullable=False, server_default='10'),
    sa.Column('relevance', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
    sa.Column('insights', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
    sa.Column('published_kpis', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
    sa.Column('opportunities', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
    sa.Column('narrative', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
    sa.Column('domains', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
    sa.Column('untracked', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
    sa.Column('checks', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('file_sha256')
    )
    op.create_table('ai_vis_row',
    sa.Column('id', BIGPK, autoincrement=True, nullable=False),
    sa.Column('import_id', sa.BigInteger(), nullable=False),
    sa.Column('row_id', sa.Integer(), nullable=False),
    sa.Column('keyword', sa.Text(), nullable=False),
    sa.Column('platform', sa.String(length=40), nullable=False),
    sa.Column('query_format', sa.String(length=40), nullable=True),
    sa.Column('topic', sa.String(length=80), nullable=False),
    sa.Column('region', sa.String(length=60), nullable=False),
    sa.Column('journey', sa.String(length=40), nullable=True),
    sa.Column('brand_type', sa.String(length=60), nullable=False),
    sa.Column('intent', sa.String(length=40), nullable=True),
    sa.Column('volume', sa.Integer(), nullable=False, server_default='0'),
    sa.Column('volume_capped', sa.Integer(), nullable=False, server_default='0'),
    sa.Column('primary_row', sa.Boolean(), nullable=False, server_default=sa.true()),
    sa.Column('excluded', sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column('metricon_mentioned', sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column('metricon_cited', sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column('gj_mentioned', sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column('carlisle_mentioned', sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column('simonds_mentioned', sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column('porter_mentioned', sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column('opportunity_type', sa.String(length=80), nullable=True),
    sa.Column('opportunity_score', sa.Float(), nullable=False, server_default='0'),
    sa.Column('competitors_named', sa.Text(), nullable=True),
    sa.Column('metricon_urls', sa.Text(), nullable=True),
    sa.Column('cited_domains', sa.Text(), nullable=True),
    sa.Column('updated_on', sa.Date(), nullable=True),
    sa.ForeignKeyConstraint(['import_id'], ['ai_vis_import.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_aivis_import_topic', 'ai_vis_row', ['import_id', 'topic'], unique=False)
    op.create_index('ix_aivis_import_region', 'ai_vis_row', ['import_id', 'region'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_aivis_import_region', table_name='ai_vis_row')
    op.drop_index('ix_aivis_import_topic', table_name='ai_vis_row')
    op.drop_table('ai_vis_row')
    op.drop_table('ai_vis_import')
