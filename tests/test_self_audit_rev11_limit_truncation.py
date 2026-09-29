"""Self-audit regression test, 2026-09-14, against commit 5134888 (R10 fix).

Not one of Ari's numbered findings by number, but reproduces his REV11-B1
measurement directly: a healthy, non-stale index with more real matches than
the query's own limit/tier cap must not force a full rebuild -- ordinary
bounded retrieval is not evidence of a corrupted index. R10-B1's completeness
fix (comparing the full real eligible-match SET against the LIMITED returned
set for exact equality) treated any truncation as a mismatch, regardless of
health. This is the fix for that regression, applied here at the internal
function level (matching Ari's own measurement, which wrapped the real repair
function and counted rebuild calls) rather than through the full MCP surface.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "src" / "agentmemory" / "db" / "init_schema.sql").read_text(encoding="utf-8")


def _healthy_db_with_n_matches(db_path, n):
    now = "2026-01-01T00:00:00"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
        "VALUES ('t','t','test','active',?,?)", (now, now),
    )
    for i in range(1, n + 1):
        conn.execute(
            "INSERT INTO memories (id, content, category, tags, indexed, agent_id, created_at, updated_at) "
            "VALUES (?,?,?,'',1,'t',?,?)", (i, f"sharedneedle row{i}", "general", now, now),
        )
        conn.execute(
            "INSERT INTO memories_fts(rowid, content, category, tags) VALUES (?,?,?,'')",
            (i, f"sharedneedle row{i}", "general"),
        )
    conn.commit()
    conn.close()


def test_healthy_truncated_results_do_not_force_a_rebuild(tmp_path):
    """Direct reproduction of Ari's REV11-B1 table: 14 real matching rows,
    limit 7 -- a perfectly healthy index, and the ONLY reason fewer than 14
    came back is the bound itself, not staleness."""
    import agentmemory.mcp_server as srv

    db_path = tmp_path / "healthy-truncated.db"
    _healthy_db_with_n_matches(db_path, 14)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    results = [
        dict(conn.execute(
            "SELECT m.* FROM memories_fts fts JOIN memories m ON m.id = fts.rowid "
            "WHERE memories_fts MATCH 'sharedneedle' ORDER BY rank LIMIT 7"
        ).fetchall()[i])
        for i in range(7)
    ]
    assert len(results) == 7

    def fetch_eligible():
        return conn.execute(
            "SELECT id, content, category, tags FROM memories WHERE retired_at IS NULL AND indexed = 1"
        ).fetchall()

    rebuild_calls = []
    real_ensure = srv._ensure_fts_index_consistent

    def counting_ensure(c):
        rebuild_calls.append(1)
        return real_ensure(c)

    import unittest.mock
    with unittest.mock.patch.object(srv, "_ensure_fts_index_consistent", counting_ensure):
        out_results, repair_failed, verification_failed = srv._verify_and_repair_stale_matches(
            conn, "sharedneedle", results,
            lambda: results, fetch_eligible, limit=7,
        )

    conn.close()
    assert not rebuild_calls, "a healthy 14-match/7-limit search must not trigger any rebuild"
    assert repair_failed is False
    assert verification_failed is False
    assert len(out_results) == 7


def test_genuine_omission_under_a_limit_is_still_caught(tmp_path):
    """Control, the other half of Ari's requirement: with the SAME 14-real-
    match/7-limit shape, if the raw index is actually missing rows (fewer
    than 7 real matches posted, despite 14 truly eligible), that must still
    be caught -- R11-B1's fix must not reopen R10-B1's completeness gap by
    over-relaxing the check."""
    import agentmemory.mcp_server as srv

    db_path = tmp_path / "genuinely-incomplete.db"
    _healthy_db_with_n_matches(db_path, 14)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Simulate a stale raw index that only posted 3 of the 14 real matches --
    # fewer than both the limit (7) and the real match count (14).
    conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('delete-all')")
    for i in range(1, 4):
        conn.execute(
            "INSERT INTO memories_fts(rowid, content, category, tags) VALUES (?,?,?,'')",
            (i, f"sharedneedle row{i}", "general"),
        )
    conn.commit()

    results = [
        dict(r) for r in conn.execute(
            "SELECT m.* FROM memories_fts fts JOIN memories m ON m.id = fts.rowid "
            "WHERE memories_fts MATCH 'sharedneedle' ORDER BY rank LIMIT 7"
        ).fetchall()
    ]
    assert len(results) == 3, "sanity: raw index really is only posting 3 of the 14 real matches"

    def fetch_eligible():
        return conn.execute(
            "SELECT id, content, category, tags FROM memories WHERE retired_at IS NULL AND indexed = 1"
        ).fetchall()

    def rerun():
        return [
            dict(r) for r in conn.execute(
                "SELECT m.* FROM memories_fts fts JOIN memories m ON m.id = fts.rowid "
                "WHERE memories_fts MATCH 'sharedneedle' ORDER BY rank LIMIT 7"
            ).fetchall()
        ]

    out_results, repair_failed, verification_failed = srv._verify_and_repair_stale_matches(
        conn, "sharedneedle", results, rerun, fetch_eligible, limit=7,
    )
    conn.close()

    assert repair_failed is False
    assert verification_failed is False
    assert len(out_results) == 7, "the rebuild must recover the full 7-slot bounded result"
