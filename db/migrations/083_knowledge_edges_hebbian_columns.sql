-- Migration 083: knowledge_edges hebbian-pass columns
--
-- last_reinforced_at, co_activation_count, and weight_updated_at have been
-- present in init_schema.sql (fresh installs) since before this migration
-- existed, but no db/migrations/*.sql file ever tracked them for upgrade
-- installs -- the only place a legacy DB picked them up was an untracked
-- runtime ALTER TABLE guard inside run_hebbian_pass (hippocampus.py),
-- executed on every single hebbian pass instead of once via `brainctl
-- migrate`. THE-65 cluster 5 (2026-07-13): closing that tracking gap here.
-- The runtime guard stays in hippocampus.py as a race-safe fallback for
-- installs that never ran `brainctl migrate` (migrations are opt-in, not
-- auto-applied -- see brain.py's _warn_if_migrations_pending), but this
-- migration is now the primary, tracked path for everyone else.
--
-- IDEMPOTENT.

ALTER TABLE knowledge_edges ADD COLUMN last_reinforced_at TEXT;
ALTER TABLE knowledge_edges ADD COLUMN co_activation_count INTEGER DEFAULT 0;
ALTER TABLE knowledge_edges ADD COLUMN weight_updated_at TEXT;

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (83, 'knowledge_edges hebbian-pass columns: last_reinforced_at, co_activation_count, weight_updated_at',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
