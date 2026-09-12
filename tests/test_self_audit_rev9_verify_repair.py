"""Self-audit regression test, 2026-09-11/12, against commit bcd3ef4.

Not one of Ari's numbered findings -- Kelly asked for a real adversarial
self-review of the R9-B1/R9-B2 fix before it went back to him ("break it
if you can"). Found this by re-applying the exact class of bug R9-B2 had
just fixed (a failed repair getting silently treated as success) to the
NEW code from that same fix: _verify_and_repair_stale_matches called
_ensure_fts_index_consistent and discarded its return value, so a real
rebuild failure (e.g. denied at the SQLite authorizer level, same
mechanism Ari used for R9-B2) still returned `ok: True` with silently
incomplete/wrong results.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "src" / "agentmemory" / "db" / "init_schema.sql").read_text(encoding="utf-8")


def _seed_stale_index_db(db_path):
    """A real content/index mismatch at the same id: memories.content says
    'wantednewtoken', but the raw FTS index still has 'obsoleteoldtoken' --
    the exact shape of Ari's R9-B1 repro, built directly rather than via a
    file-replace, since this test cares about the rebuild call itself, not
    how staleness was introduced."""
    now = "2026-01-01T00:00:00"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
        "VALUES ('t','t','test','active',?,?)", (now, now),
    )
    conn.execute(
        "INSERT INTO memories (id, content, category, tags, indexed, agent_id, created_at, updated_at) "
        "VALUES (1,'wantednewtoken','general','',1,'t',?,?)", (now, now),
    )
    conn.commit()
    conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('delete-all')")
    conn.execute(
        "INSERT INTO memories_fts(rowid,content,category,tags) VALUES(1,'obsoleteoldtoken','general','')"
    )
    conn.commit()
    conn.close()


def test_failed_repair_inside_verify_path_is_surfaced_not_swallowed(tmp_path, monkeypatch):
    """A rebuild denied at the SQLite authorizer level, triggered by the
    R9-B1 verify-and-repair path itself (not the earlier _cold_start_check_once
    gate -- this deliberately never lets that gate run its own rebuild, so
    the ONLY rebuild attempt in this whole call happens inside
    _verify_and_repair_stale_matches). Before this fix: `ok: True, count: 0`,
    silently indistinguishable from a genuine "nothing found." After: the
    same silent-content result, but `index_repair_failed: True` present so
    a caller can tell the difference."""
    import agentmemory.mcp_server as srv

    db_path = tmp_path / "stale.db"
    _seed_stale_index_db(db_path)

    monkeypatch.setattr(srv, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(srv, "_DB_PATH_LOCKED", True, raising=False)
    srv._FTS_REBUILD_CHECKED_PATHS.clear()

    real_get_db = srv.get_db

    def denying_get_db():
        conn = real_get_db()

        def auth(action, arg1, arg2, database, trigger):
            if action == sqlite3.SQLITE_INSERT and arg1 == "memories_fts":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(auth)
        return conn

    monkeypatch.setattr(srv, "get_db", denying_get_db)

    result = srv.tool_memory_search(agent_id="t", query="wantednewtoken", benchmark=True)

    assert result["ok"] is True, "search itself must not raise/crash on a denied rebuild"
    assert result["count"] == 0, (
        "sanity: with the rebuild denied, the real content genuinely can't be "
        "recovered this call -- the point of this test is the flag, not a miracle fix"
    )
    assert result.get("index_repair_failed") is True, (
        "a real content/index mismatch was detected AND the resulting rebuild "
        "failed -- that must be surfaced, not silently reported as an ordinary "
        "successful zero-result search"
    )


def test_successful_repair_does_not_set_the_failure_flag(tmp_path, monkeypatch):
    """Control: the exact same stale-index scenario, WITHOUT the authorizer
    denial, must repair cleanly and never set index_repair_failed."""
    import agentmemory.mcp_server as srv

    db_path = tmp_path / "stale.db"
    _seed_stale_index_db(db_path)

    monkeypatch.setattr(srv, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(srv, "_DB_PATH_LOCKED", True, raising=False)
    srv._FTS_REBUILD_CHECKED_PATHS.clear()

    result = srv.tool_memory_search(agent_id="t", query="wantednewtoken", benchmark=True)

    assert result["ok"] is True
    assert result["count"] == 1, "an unimpeded rebuild must actually recover the real content"
    assert result["memories"][0]["id"] == 1
    assert "index_repair_failed" not in result, (
        "a successful repair must not carry a failure flag forward"
    )
