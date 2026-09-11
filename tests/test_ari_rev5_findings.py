"""Regression tests for Ari's independent adversarial REV5 findings
(2026-09-10), against commit 28aed0b.

FINDINGS_REV5.md: F:\\GPT Codex\\reviews\\brainctl-fts-fix-2026-09-10\\FINDINGS_REV5.md
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "src" / "agentmemory" / "db" / "init_schema.sql").read_text(encoding="utf-8")


def _write_db(db_path, eligible_present_in_fts: bool):
    now = "2026-09-10T12:00:00"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
        "VALUES ('t','t','test','active',?,?)", (now, now),
    )
    conn.execute(
        "INSERT INTO memories (id, content, category, tags, indexed, agent_id, created_at, updated_at) "
        "VALUES (1,'findable content','general','',1,'t',?,?)", (now, now),
    )
    conn.commit()
    if not eligible_present_in_fts:
        conn.execute("DELETE FROM memories_fts")
        conn.commit()
    return conn


# --- R5-B1: a restored/copied database carrying the same stamp must not be trusted forever ---

def test_r5_b1_stamp_collision_self_heals_after_ttl(tmp_path, monkeypatch):
    """Ari's finding: the R3-B1 stamp fix trusts a stamp forever, but a
    restored backup or cloned file carries the SAME stamp forward -- it's
    the same bytes. No passive signal can tell "still the live database"
    apart from "an old snapshot of it, restored later." The real fix bounds
    the resulting staleness with a TTL rather than chasing a perfect
    identity signal. This test shrinks the TTL so it doesn't need to sleep
    300 real seconds to prove the bound actually works."""
    import agentmemory.mcp_server as srv

    if not hasattr(srv, "_cold_start_check_once") or not hasattr(srv, "_db_instance_id"):
        pytest.skip("_cold_start_check_once/_db_instance_id don't exist on this commit -- new functionality")

    monkeypatch.setattr(srv, "_FTS_REBUILD_CHECK_TTL_SECONDS", 0.2, raising=False)
    srv._FTS_REBUILD_CHECKED_PATHS.clear()

    db_path = tmp_path / "shared-name.db"

    conn1 = _write_db(db_path, eligible_present_in_fts=True)
    assert srv._cold_start_check_once(conn1, db_path) is True  # first check, stamps the instance id
    stamp = conn1.execute(
        "SELECT value FROM workspace_config WHERE key = '_db_instance_id'"
    ).fetchone()[0]
    conn1.close()

    # Simulate a restored backup: a genuinely different, underpopulated
    # database that happens to carry the SAME stamp forward (exactly what a
    # real file copy/restore would do -- the stamp travels with the bytes).
    db_path.unlink()
    conn2 = _write_db(db_path, eligible_present_in_fts=False)
    conn2.execute(
        "INSERT INTO workspace_config (key, value) VALUES ('_db_instance_id', ?)", (stamp,)
    )
    conn2.commit()

    # Immediately after: still within the TTL window, so the stamp
    # collision is (correctly, by design) still trusted -- this isn't the
    # bug, it's the intentional bound.
    assert srv._cold_start_check_once(conn2, db_path) is False

    # Past the TTL: the same stamp collision must now self-heal.
    time.sleep(0.25)
    ran = srv._cold_start_check_once(conn2, db_path)
    assert ran is True, (
        "a stamp collision from a restored/copied database must self-heal "
        "once the TTL window has passed (R5-B1)"
    )
    assert set(r[0] for r in conn2.execute("SELECT rowid FROM memories_fts_docsize").fetchall()) == {1}


# --- R5-B2: two sequential environment-only changes must both take effect ---

def test_r5_b2_second_env_only_change_still_takes_effect(monkeypatch, tmp_path):
    """Ari's finding: _DB_PATH_DEFAULT was a single frozen import-time value,
    so after the FIRST legitimate environment-only re-derivation, DB_PATH no
    longer equals it -- making a SECOND environment-only change (BRAIN_DB=A,
    then later BRAIN_DB=B, same process) look identical to "someone
    explicitly pinned DB_PATH," and get_db() kept opening A. This calls the
    real get_db() twice in sequence, through two different BRAIN_DB values,
    never touching DB_PATH directly -- exactly the natural pattern Ari's
    report asked for."""
    import agentmemory._impl as impl

    if not hasattr(impl, "_DB_PATH_DEFAULT"):
        pytest.skip("_DB_PATH_DEFAULT doesn't exist on this commit -- new functionality")

    a = tmp_path / "a.db"
    b = tmp_path / "b.db"
    for p in (a, b):
        _write_db(p, eligible_present_in_fts=True).close()

    monkeypatch.setattr(impl, "_DB_PATH_LOCKED", False, raising=False)
    monkeypatch.setattr(impl, "DB_PATH", impl._DB_PATH_DEFAULT, raising=False)

    monkeypatch.setenv("BRAIN_DB", str(a))
    conn = impl.get_db()
    conn.close()
    assert impl.DB_PATH == a, "first environment-only change must take effect"

    monkeypatch.setenv("BRAIN_DB", str(b))
    conn = impl.get_db()
    conn.close()
    assert impl.DB_PATH == b, (
        "a second, later environment-only change in the same process must "
        "also take effect, not stick to the first one (R5-B2)"
    )
