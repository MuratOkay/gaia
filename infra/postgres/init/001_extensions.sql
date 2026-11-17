-- ---------------------------------------------------------------------------
-- Gaia metadata store extensions.
-- Runs only on first initialization of an empty data volume.
-- To re-run: `make reset-db` (destroys the volume).
-- ---------------------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS btree_gin;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Application schemas. Owned by the bootstrap role; Alembic manages objects.
CREATE SCHEMA IF NOT EXISTS catalog;
CREATE SCHEMA IF NOT EXISTS profiling;
CREATE SCHEMA IF NOT EXISTS semantics;
CREATE SCHEMA IF NOT EXISTS chat;
CREATE SCHEMA IF NOT EXISTS evaluation;
CREATE SCHEMA IF NOT EXISTS audit;
CREATE SCHEMA IF NOT EXISTS auth;

COMMENT ON SCHEMA catalog    IS 'Raw and normalized target-database metadata snapshots';
COMMENT ON SCHEMA profiling  IS 'Table and column profile measurements';
COMMENT ON SCHEMA semantics  IS 'Reasoned semantic models, scores, embeddings';
COMMENT ON SCHEMA chat       IS 'Conversations, messages, generated query runs';
COMMENT ON SCHEMA evaluation IS 'Golden questions and evaluation results';
COMMENT ON SCHEMA audit      IS 'LLM call log and detected schema changes';
COMMENT ON SCHEMA auth       IS 'Directories, accounts, roles, grants, sessions';
