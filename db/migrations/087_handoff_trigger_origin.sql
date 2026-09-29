-- Migration 087: provenance ("origin") on handoff packets and memory triggers
-- (Cairn trigger/handoff persistence gap, item 1 of 4, 2026-09-29)
--
-- Found 2026-09-22 in a backward-adversary audit: trigger_create + trigger_check,
-- and handoff_add + handoff_latest/agent_orient, are ungated plant-and-surface
-- chains. Text written mid-session by an agent (possibly while steered by a
-- prompt injection) resurfaces at the next boot with nothing recording where it
-- came from, so it reads as the agent's own settled prior judgment.
--
-- Signing cannot fix this: an injected write goes through the same tool as a
-- real one. What CAN be done is to record which write path produced each row
-- and make the read side say so.
--
-- origin values: 'wrap_up' (Brain.wrap_up / brainctl_wrapup), 'direct' (MCP
-- tool call), 'cli' (brainctl command line), 'api' (Brain Python API),
-- 'legacy' (row predates this migration; provenance unknown).
--
-- Rollback:
--   ALTER TABLE handoff_packets DROP COLUMN origin;
--   ALTER TABLE memory_triggers DROP COLUMN origin;
--   DELETE FROM schema_version WHERE version = 87;
--   DELETE FROM schema_versions WHERE version = 87;

ALTER TABLE handoff_packets ADD COLUMN origin TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE memory_triggers ADD COLUMN origin TEXT NOT NULL DEFAULT 'legacy';

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (87, 'origin column on handoff_packets and memory_triggers (write-path provenance)',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
