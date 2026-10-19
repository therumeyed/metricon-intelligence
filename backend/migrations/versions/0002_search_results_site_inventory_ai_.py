"""search results, site inventory, ai analysis, feeds

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('insight_report',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('scope', sa.String(length=3), nullable=False),
    sa.Column('week_start', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('is_current', sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column('generated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('model', sa.String(length=80), nullable=True),
    sa.Column('prompt_version', sa.String(length=30), nullable=True),
    sa.Column('facts', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('report', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_insight_scope_week', 'insight_report', ['scope', 'week_start'], unique=False)
    op.create_table('news_feed',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('label', sa.String(length=160), nullable=True),
    sa.Column('state', sa.String(length=3), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.true()),
    sa.Column('last_fetched_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('items_last_run', sa.Integer(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('url')
    )
    op.create_table('site_page',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('sources', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('page_kind', sa.String(length=40), nullable=True),
    sa.Column('theme_slug', sa.String(length=60), nullable=True),
    sa.Column('state_hint', sa.String(length=3), nullable=True),
    sa.Column('impressions', sa.BigInteger(), nullable=True),
    sa.Column('clicks', sa.BigInteger(), nullable=True),
    sa.Column('position', sa.Float(), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('url')
    )
    op.create_index('ix_site_page_kind', 'site_page', ['page_kind'], unique=False)
    op.add_column('competitor_event', sa.Column('origin', sa.String(length=16), nullable=False, server_default='crawl'))
    op.add_column('competitor_event', sa.Column('published_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('offer', sa.Column('evidence_level', sa.String(length=16), nullable=False, server_default='page'))
    op.add_column('trend_plan', sa.Column('auto_managed', sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column('trend_plan', 'auto_managed')
    op.drop_column('offer', 'evidence_level')
    op.drop_column('competitor_event', 'published_at')
    op.drop_column('competitor_event', 'origin')
    op.drop_index('ix_site_page_kind', table_name='site_page')
    op.drop_table('site_page')
    op.drop_table('news_feed')
    op.drop_index('ix_insight_scope_week', table_name='insight_report')
    op.drop_table('insight_report')
