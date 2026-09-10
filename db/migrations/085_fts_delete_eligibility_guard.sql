-- Migration 085: guard every memories_fts delete trigger with eligibility
-- (Ari's independent adversarial review, REV1, findings F5/F8, 2026-09-10)
--
-- memories_fts_update_delete and memories_fts_delete both attempted an
-- FTS5 external-content 'delete' whenever old.indexed = 1, with no check
-- that the row was actually PRESENT in the raw index at that moment. A
-- retired (or otherwise already-removed) row is not present, and deleting
-- an absent external-content row is unsafe: reproduced both as a silent
-- no-restore on unretirement (F5: active -> retired -> active never got
-- the memory searchable again) and as outright corruption
-- ("database disk image is malformed", F8) when two such deletes landed
-- in one transaction. Root cause is the same in both triggers: neither
-- checked old.retired_at, only old.indexed.
--
-- Fix: both delete-side triggers now also require old.retired_at IS NULL,
-- matching the eligibility guard the insert-side triggers already used.
-- memories_fts_insert (AFTER INSERT) gets the same treatment for a
-- related gap (F6): it checked new.indexed = 1 but not
-- new.retired_at IS NULL, so a row inserted already-retired was
-- immediately token-searchable in raw FTS.
--
-- Verified via disposable in-memory reproduction before this migration
-- was written: with the old triggers, active -> retired -> active left
-- the memory unsearchable, and a two-row delete sequence corrupted the
-- database; with the corrected WHEN clauses, both cases pass clean.
--
-- Rollback:
--   restore the pre-085 trigger bodies (unconditional AFTER DELETE,
--   old.indexed = 1 only on update-delete, new.indexed = 1 only on insert)
--   DELETE FROM schema_version WHERE version = 85;
--
-- IDEMPOTENT.

DROP TRIGGER IF EXISTS memories_fts_insert;
DROP TRIGGER IF EXISTS memories_fts_update_delete;
DROP TRIGGER IF EXISTS memories_fts_delete;

CREATE TRIGGER memories_fts_insert AFTER INSERT ON memories WHEN new.indexed = 1 AND new.retired_at IS NULL BEGIN
    INSERT INTO memories_fts(rowid, content, category, tags) VALUES (new.id, new.content, new.category, new.tags);
END;

CREATE TRIGGER memories_fts_update_delete AFTER UPDATE OF content, category, tags, indexed, retired_at ON memories WHEN old.indexed = 1 AND old.retired_at IS NULL BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, category, tags)
    VALUES ('delete', old.id, old.content, old.category, old.tags);
END;

CREATE TRIGGER memories_fts_delete AFTER DELETE ON memories WHEN old.indexed = 1 AND old.retired_at IS NULL BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, category, tags) VALUES('delete', old.id, old.content, old.category, old.tags);
END;

-- Rebuild + purge, same pattern as 083/084: a prior bad delete/insert could
-- have left raw FTS in an inconsistent state relative to eligibility.
INSERT INTO memories_fts(memories_fts) VALUES('rebuild');

INSERT INTO memories_fts(memories_fts, rowid, content, category, tags)
SELECT 'delete', m.id, m.content, m.category, m.tags
FROM memories m JOIN memories_fts_docsize d ON d.rowid = m.id
WHERE NOT (m.indexed = 1 AND m.retired_at IS NULL);

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (85, 'guard memories_fts delete triggers with retired_at eligibility (F5/F8)',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
