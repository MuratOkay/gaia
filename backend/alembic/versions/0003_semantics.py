"""Semantics: versioned meaning, scores, join hints, glossary, embeddings, LLM audit.

Phase 2 (PHASES.md). Creates in ``semantics``: ``reasoning_run``,
``table_semantic``, ``column_semantic``, ``join_hint``, ``glossary_term``,
``embedding``; and in ``audit``: ``llm_call``.

Requires the ``vector`` and ``pg_trgm`` extensions from ``0001``:
``embedding.embedding`` is ``vector(1024)`` with an HNSW cosine index
(``bge-m3``, OUTLINE §15), and trigram GIN indexes back fuzzy name search.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('glossary_term',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('data_source_id', sa.Uuid(), nullable=True),
    sa.Column('term', sa.Text(), nullable=False),
    sa.Column('definition', sa.Text(), nullable=False),
    sa.Column('synonyms', postgresql.ARRAY(sa.Text()), nullable=False),
    sa.Column('locale', sa.Text(), nullable=False),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("source IN ('sme', 'import')", name=op.f('ck_glossary_term_source')),
    sa.CheckConstraint("status IN ('proposed', 'approved', 'rejected')", name=op.f('ck_glossary_term_status')),
    sa.ForeignKeyConstraint(['data_source_id'], ['catalog.data_sources.id'], name=op.f('fk_glossary_term_data_source_id_data_sources'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_glossary_term')),
    schema='semantics'
    )
    op.create_index('ix_glossary_term_term_trgm', 'glossary_term', ['term'], unique=False, schema='semantics', postgresql_using='gin', postgresql_ops={'term': 'gin_trgm_ops'})
    op.create_index('uq_glossary_term_scope_term_locale', 'glossary_term', [sa.literal_column("coalesce(data_source_id, '00000000-0000-0000-0000-000000000000'::uuid)"), sa.literal_column('lower(term)'), 'locale'], unique=True, schema='semantics')
    op.create_table('reasoning_run',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('data_source_id', sa.Uuid(), nullable=False),
    sa.Column('snapshot_id', sa.Uuid(), nullable=True),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('prompt_version', sa.Text(), nullable=False),
    sa.Column('critique_prompt_version', sa.Text(), nullable=False),
    sa.Column('thinking', sa.Boolean(), nullable=False),
    sa.Column('critique_thinking', sa.Boolean(), nullable=False),
    sa.Column('options', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('tables_in_scope', sa.Integer(), nullable=False),
    sa.Column('tables_reasoned', sa.Integer(), nullable=False),
    sa.Column('tables_skipped', sa.Integer(), nullable=False),
    sa.Column('tables_failed', sa.Integer(), nullable=False),
    sa.Column('critiques_failed', sa.Integer(), nullable=False),
    sa.Column('columns_written', sa.Integer(), nullable=False),
    sa.Column('embeddings_written', sa.Integer(), nullable=False),
    sa.Column('llm_calls', sa.Integer(), nullable=False),
    sa.Column('cache_hits', sa.Integer(), nullable=False),
    sa.Column('prompt_tokens', sa.BigInteger(), nullable=False),
    sa.Column('completion_tokens', sa.BigInteger(), nullable=False),
    sa.Column('reasoning_tokens', sa.BigInteger(), nullable=False),
    sa.Column('cached_tokens', sa.BigInteger(), nullable=False),
    sa.Column('wall_ms', sa.BigInteger(), nullable=True),
    sa.Column('notes', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.CheckConstraint("status IN ('running', 'completed', 'completed_with_failures', 'failed')", name=op.f('ck_reasoning_run_status')),
    sa.ForeignKeyConstraint(['data_source_id'], ['catalog.data_sources.id'], name=op.f('fk_reasoning_run_data_source_id_data_sources'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['snapshot_id'], ['catalog.snapshots.id'], name=op.f('fk_reasoning_run_snapshot_id_snapshots'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_reasoning_run')),
    schema='semantics'
    )
    op.create_index('ix_reasoning_run_source_started', 'reasoning_run', ['data_source_id', 'started_at'], unique=False, schema='semantics')
    op.create_table('llm_call',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=True),
    sa.Column('data_source_id', sa.Uuid(), nullable=True),
    sa.Column('table_id', sa.Uuid(), nullable=True),
    sa.Column('purpose', sa.Text(), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('prompt_version', sa.Text(), nullable=False),
    sa.Column('prompt_hash', sa.Text(), nullable=False),
    sa.Column('thinking', sa.Boolean(), nullable=False),
    sa.Column('json_mode', sa.Boolean(), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('cache_hit', sa.Boolean(), nullable=False),
    sa.Column('parsed_ok', sa.Boolean(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('finish_reason', sa.Text(), nullable=True),
    sa.Column('request_id', sa.Text(), nullable=True),
    sa.Column('prompt_tokens', sa.Integer(), nullable=False),
    sa.Column('completion_tokens', sa.Integer(), nullable=False),
    sa.Column('reasoning_tokens', sa.Integer(), nullable=False),
    sa.Column('cached_tokens', sa.Integer(), nullable=False),
    sa.Column('total_tokens', sa.Integer(), nullable=False),
    sa.Column('latency_ms', sa.Integer(), nullable=False),
    sa.Column('transport_attempts', sa.Integer(), nullable=False),
    sa.Column('cost', sa.Numeric(precision=12, scale=6), nullable=True),
    sa.Column('raw_response', sa.Text(), nullable=True),
    sa.Column('reasoning', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['data_source_id'], ['catalog.data_sources.id'], name=op.f('fk_llm_call_data_source_id_data_sources'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['run_id'], ['semantics.reasoning_run.id'], name=op.f('fk_llm_call_run_id_reasoning_run'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['table_id'], ['catalog.tables.id'], name=op.f('fk_llm_call_table_id_tables'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_llm_call')),
    schema='audit'
    )
    op.create_index('ix_llm_call_created', 'llm_call', ['created_at'], unique=False, schema='audit')
    op.create_index('ix_llm_call_prompt_hash', 'llm_call', ['prompt_hash'], unique=False, schema='audit')
    op.create_index('ix_llm_call_run', 'llm_call', ['run_id'], unique=False, schema='audit')
    op.create_table('join_hint',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('data_source_id', sa.Uuid(), nullable=False),
    sa.Column('from_schema', sa.Text(), nullable=False),
    sa.Column('from_table', sa.Text(), nullable=False),
    sa.Column('from_columns', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('to_schema', sa.Text(), nullable=False),
    sa.Column('to_table', sa.Text(), nullable=False),
    sa.Column('to_columns', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('relation_name', sa.Text(), nullable=True),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('rationale', sa.Text(), nullable=True),
    sa.Column('run_id', sa.Uuid(), nullable=True),
    sa.Column('prompt_version', sa.Text(), nullable=True),
    sa.Column('model', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("source IN ('fk', 'llm', 'sme')", name=op.f('ck_join_hint_source')),
    sa.CheckConstraint("status IN ('confirmed', 'inferred', 'rejected', 'stale')", name=op.f('ck_join_hint_status')),
    sa.ForeignKeyConstraint(['data_source_id'], ['catalog.data_sources.id'], name=op.f('fk_join_hint_data_source_id_data_sources'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['run_id'], ['semantics.reasoning_run.id'], name=op.f('fk_join_hint_run_id_reasoning_run'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_join_hint')),
    sa.UniqueConstraint('data_source_id', 'from_schema', 'from_table', 'from_columns', 'to_schema', 'to_table', 'to_columns', name='uq_join_hint_path'),
    schema='semantics'
    )
    op.create_index('ix_join_hint_from', 'join_hint', ['data_source_id', 'from_schema', 'from_table'], unique=False, schema='semantics')
    op.create_index('ix_join_hint_to', 'join_hint', ['data_source_id', 'to_schema', 'to_table'], unique=False, schema='semantics')
    op.create_table('table_semantic',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('data_source_id', sa.Uuid(), nullable=False),
    sa.Column('table_id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=True),
    sa.Column('snapshot_id', sa.Uuid(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('is_current', sa.Boolean(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('locale', sa.Text(), nullable=False),
    sa.Column('structure_hash', sa.Text(), nullable=False),
    sa.Column('prompt_version', sa.Text(), nullable=False),
    sa.Column('critique_prompt_version', sa.Text(), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('thinking', sa.Boolean(), nullable=False),
    sa.Column('business_name', sa.Text(), nullable=False),
    sa.Column('purpose', sa.Text(), nullable=False),
    sa.Column('grain', sa.Text(), nullable=True),
    sa.Column('entity', sa.Text(), nullable=True),
    sa.Column('synonyms', postgresql.ARRAY(sa.Text()), nullable=False),
    sa.Column('assumptions', postgresql.ARRAY(sa.Text()), nullable=False),
    sa.Column('unknowns', postgresql.ARRAY(sa.Text()), nullable=False),
    sa.Column('self_confidence', sa.Float(), nullable=False),
    sa.Column('raw_score', sa.Float(), nullable=True),
    sa.Column('calibrated_score', sa.Float(), nullable=True),
    sa.Column('score_breakdown', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('critique', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('draft', 'auto_approved', 'sme_reviewed', 'sme_rejected', 'stale')", name=op.f('ck_table_semantic_status')),
    sa.ForeignKeyConstraint(['data_source_id'], ['catalog.data_sources.id'], name=op.f('fk_table_semantic_data_source_id_data_sources'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['run_id'], ['semantics.reasoning_run.id'], name=op.f('fk_table_semantic_run_id_reasoning_run'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['snapshot_id'], ['catalog.snapshots.id'], name=op.f('fk_table_semantic_snapshot_id_snapshots'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['table_id'], ['catalog.tables.id'], name=op.f('fk_table_semantic_table_id_tables'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_table_semantic')),
    sa.UniqueConstraint('table_id', 'version', name='uq_table_semantic_table_version'),
    schema='semantics'
    )
    op.create_index('ix_table_semantic_source_current', 'table_semantic', ['data_source_id', 'is_current'], unique=False, schema='semantics')
    op.create_index('uq_table_semantic_current', 'table_semantic', ['table_id'], unique=True, schema='semantics', postgresql_where=sa.text('is_current'))
    op.create_table('column_semantic',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('table_semantic_id', sa.Uuid(), nullable=False),
    sa.Column('table_id', sa.Uuid(), nullable=False),
    sa.Column('column_id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('is_current', sa.Boolean(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('band', sa.Text(), nullable=False),
    sa.Column('locale', sa.Text(), nullable=False),
    sa.Column('structure_hash', sa.Text(), nullable=False),
    sa.Column('table_structure_hash', sa.Text(), nullable=False),
    sa.Column('prompt_version', sa.Text(), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('business_name', sa.Text(), nullable=False),
    sa.Column('purpose', sa.Text(), nullable=False),
    sa.Column('role', sa.Text(), nullable=False),
    sa.Column('entity', sa.Text(), nullable=True),
    sa.Column('unit', sa.Text(), nullable=True),
    sa.Column('value_domain', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('synonyms', postgresql.ARRAY(sa.Text()), nullable=False),
    sa.Column('example_questions', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('assumptions', postgresql.ARRAY(sa.Text()), nullable=False),
    sa.Column('unknowns', postgresql.ARRAY(sa.Text()), nullable=False),
    sa.Column('alternatives', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('evidence_refs', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('self_confidence', sa.Float(), nullable=False),
    sa.Column('critique', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('raw_score', sa.Float(), nullable=False),
    sa.Column('calibrated_score', sa.Float(), nullable=False),
    sa.Column('score_breakdown', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('weights_version', sa.Text(), nullable=False),
    sa.Column('calibrator', sa.Text(), nullable=False),
    sa.Column('pii_types', postgresql.ARRAY(sa.Text()), nullable=True),
    sa.Column('pii_confidence', sa.Float(), nullable=True),
    sa.Column('pii_evidence', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('search_vector', postgresql.TSVECTOR(), sa.Computed("to_tsvector('english'::regconfig, coalesce(business_name, '') || ' ' || coalesce(purpose, '') || ' ' || coalesce(entity, ''))", persisted=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("band IN ('accepted', 'deep_profile', 'deep_profile_flagged', 'abstain')", name=op.f('ck_column_semantic_band')),
    sa.CheckConstraint("status IN ('draft', 'auto_approved', 'sme_reviewed', 'sme_rejected', 'stale')", name=op.f('ck_column_semantic_status')),
    sa.ForeignKeyConstraint(['column_id'], ['catalog.columns.id'], name=op.f('fk_column_semantic_column_id_columns'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['run_id'], ['semantics.reasoning_run.id'], name=op.f('fk_column_semantic_run_id_reasoning_run'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['table_id'], ['catalog.tables.id'], name=op.f('fk_column_semantic_table_id_tables'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['table_semantic_id'], ['semantics.table_semantic.id'], name=op.f('fk_column_semantic_table_semantic_id_table_semantic'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_column_semantic')),
    sa.UniqueConstraint('column_id', 'version', name='uq_column_semantic_column_version'),
    schema='semantics'
    )
    op.create_index('ix_column_semantic_band', 'column_semantic', ['band'], unique=False, schema='semantics')
    op.create_index('ix_column_semantic_business_name_trgm', 'column_semantic', ['business_name'], unique=False, schema='semantics', postgresql_using='gin', postgresql_ops={'business_name': 'gin_trgm_ops'})
    op.create_index('ix_column_semantic_search', 'column_semantic', ['search_vector'], unique=False, schema='semantics', postgresql_using='gin')
    op.create_index('ix_column_semantic_table_current', 'column_semantic', ['table_id', 'is_current'], unique=False, schema='semantics')
    op.create_index('uq_column_semantic_current', 'column_semantic', ['column_id'], unique=True, schema='semantics', postgresql_where=sa.text('is_current'))
    op.create_table('embedding',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('subject_kind', sa.Text(), nullable=False),
    sa.Column('table_semantic_id', sa.Uuid(), nullable=True),
    sa.Column('column_semantic_id', sa.Uuid(), nullable=True),
    sa.Column('table_id', sa.Uuid(), nullable=False),
    sa.Column('column_id', sa.Uuid(), nullable=True),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('content_hash', sa.Text(), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('dimensions', sa.Integer(), nullable=False),
    sa.Column('embedding', Vector(1024), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(subject_kind = 'table' AND table_semantic_id IS NOT NULL AND column_semantic_id IS NULL) OR (subject_kind = 'column' AND column_semantic_id IS NOT NULL AND table_semantic_id IS NULL)", name=op.f('ck_embedding_subject')),
    sa.ForeignKeyConstraint(['column_id'], ['catalog.columns.id'], name=op.f('fk_embedding_column_id_columns'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['column_semantic_id'], ['semantics.column_semantic.id'], name=op.f('fk_embedding_column_semantic_id_column_semantic'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['table_id'], ['catalog.tables.id'], name=op.f('fk_embedding_table_id_tables'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['table_semantic_id'], ['semantics.table_semantic.id'], name=op.f('fk_embedding_table_semantic_id_table_semantic'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_embedding')),
    schema='semantics'
    )
    op.create_index('ix_embedding_vector_hnsw', 'embedding', ['embedding'], unique=False, schema='semantics', postgresql_using='hnsw', postgresql_with={'m': 16, 'ef_construction': 64}, postgresql_ops={'embedding': 'vector_cosine_ops'})
    op.create_index('uq_embedding_column_semantic_model', 'embedding', ['column_semantic_id', 'model'], unique=True, schema='semantics', postgresql_where=sa.text('column_semantic_id IS NOT NULL'))
    op.create_index('uq_embedding_table_semantic_model', 'embedding', ['table_semantic_id', 'model'], unique=True, schema='semantics', postgresql_where=sa.text('table_semantic_id IS NOT NULL'))


def downgrade() -> None:
    # Indexes are dropped with their tables.
    op.drop_table('embedding', schema='semantics')
    op.drop_table('column_semantic', schema='semantics')
    op.drop_table('table_semantic', schema='semantics')
    op.drop_table('join_hint', schema='semantics')
    op.drop_table('llm_call', schema='audit')
    op.drop_table('reasoning_run', schema='semantics')
    op.drop_table('glossary_term', schema='semantics')
