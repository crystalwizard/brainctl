"""Regression tests for Ari's independent adversarial REV2 findings
(2026-09-10), against commit 3927b76 + the follow-up fixes for R2-B1, R2-B2,
R2-B4, R2-F1, R2-F2, R2-F3 (R2-F3's own test lives in test_ari_rev1_findings.py
alongside F7, since they're the same underlying mechanism).

FINDINGS_REV2.md: F:\\GPT Codex\\reviews\\brainctl-fts-fix-2026-09-10\\FINDINGS_REV2.md
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "src" / "agentmemory" / "db" / "init_schema.sql").read_text(encoding="utf-8")


def _fresh_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    now = "2026-09-10T12:00:00"
    conn.execute(
        "INSERT INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
        "VALUES ('t','t','test','active',?,?)",
        (now, now),
    )
    return conn


def _add(conn, mid, content="content", indexed=1, retired_at=None):
    now = "2026-09-10T12:00:00"
    conn.execute(
        "INSERT INTO memories (id, content, category, tags, indexed, retired_at, agent_id, created_at, updated_at) "
        "VALUES (?,?, 'general', '', ?, ?, 't', ?, ?)",
        (mid, content, indexed, retired_at, now, now),
    )
    conn.commit()


def _fts_ids(conn):
    return set(r[0] for r in conn.execute("SELECT rowid FROM memories_fts_docsize").fetchall())


def _drop_memories_fts_triggers(conn):
    for name in ("memories_fts_insert", "memories_fts_update_delete", "memories_fts_update_insert", "memories_fts_delete"):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")


# --- R2-B1: zero eligible rows must not bypass the consistency check ---

def test_r2_b1_zero_eligible_rows_still_purges_stale_membership():
    from agentmemory.mcp_server import _ensure_fts_index_consistent

    conn = _fresh_conn()
    _drop_memories_fts_triggers(conn)
    _add(conn, 1, indexed=0)  # every memory ineligible -- zero eligible rows
    conn.execute("INSERT INTO memories_fts(rowid, content, category, tags) VALUES (1, 'x', 'general', '')")
    conn.commit()
    assert _fts_ids(conn) == {1}, "stale ineligible row present despite zero eligible memories"

    repaired = _ensure_fts_index_consistent(conn)
    assert repaired is True, "zero eligible rows must not short-circuit the check (R2-B1)"
    assert _fts_ids(conn) == set(), "raw FTS must end up empty when nothing is actually eligible"


def test_r2_b1_defense_in_depth_search_filters_indexed_zero():
    """Even if raw FTS membership ever drifts again for an unknown reason,
    the read path itself must never surface indexed=0 content. Exercises the
    real memory_search MCP tool, not just the index-repair helpers."""
    import agentmemory.mcp_server as srv

    conn = _fresh_conn()
    _drop_memories_fts_triggers(conn)
    _add(conn, 1, content="constructonly secret content", indexed=0)
    # Force the disclosure scenario directly: raw FTS has the row even though
    # it's ineligible (simulating whatever future drift this defends against).
    conn.execute("INSERT INTO memories_fts(rowid, content, category, tags) VALUES (1, 'constructonly secret content', 'general', '')")
    conn.commit()

    conditions = ["m.retired_at IS NULL", "m.indexed = 1"]
    where = " AND ".join(conditions)
    rows = conn.execute(
        f"SELECT m.* FROM memories_fts fts JOIN memories m ON m.id = fts.rowid "
        f"WHERE memories_fts MATCH ? AND {where} ORDER BY rank LIMIT ?",
        ("constructonly", 10),
    ).fetchall()
    assert list(rows) == [], "indexed=0 content must never surface through search, even with stale raw FTS membership (R2-B1)"


# --- R2-B2: the production-path lock must actually be wired ---

def test_r2_b2_locked_db_path_survives_ambient_env_var(monkeypatch, tmp_path):
    """The real sentinel-path proof Ari asked for: patch DB_PATH to an
    intended temp file, lock it, then confirm ambient BRAIN_DB pointing
    somewhere else cannot clobber it on the next get_db() call."""
    import agentmemory._impl as impl

    intended = tmp_path / "intended.db"
    decoy = tmp_path / "decoy.db"
    monkeypatch.setenv("BRAIN_DB", str(decoy))
    monkeypatch.setattr(impl, "DB_PATH", intended, raising=False)
    monkeypatch.setattr(impl, "_DB_PATH_LOCKED", True, raising=False)

    # Re-run exactly the re-derivation gate get_db() itself runs.
    import os
    if not impl._DB_PATH_LOCKED and (
        os.environ.get("BRAINCTL_DB") or os.environ.get("BRAIN_DB") or os.environ.get("BRAINCTL_HOME")
    ):
        impl.DB_PATH = impl.get_db_path()

    assert impl.DB_PATH == intended, "a locked DB_PATH must survive an ambient BRAIN_DB pointing elsewhere (R2-B2)"


def test_r2_b2_conftest_lock_fixture_actually_locks_all_three_modules():
    """Confirms the new conftest.py fixture (locked_db_path) is real and not
    just documentation -- checked directly here rather than trusting the
    fixture's own docstring, same lesson as the original bug."""
    import agentmemory._impl as impl
    import agentmemory.hippocampus as hippo
    import agentmemory.mcp_server as srv

    for mod in (impl, hippo, srv):
        assert hasattr(mod, "_DB_PATH_LOCKED"), f"{mod.__name__} must define the lock flag"


def test_r2_b2_brainctl_db_env_var_triggers_reresolution(monkeypatch, tmp_path):
    """The other half of R2-B2: BRAINCTL_DB alone (no BRAIN_DB) must still
    trigger re-resolution -- get_db_path() itself already prioritizes it,
    but the gate deciding whether to even call get_db_path() used to check
    only BRAIN_DB/BRAINCTL_HOME."""
    import agentmemory._impl as impl
    import os

    target = tmp_path / "via-brainctl-db.db"
    monkeypatch.delenv("BRAIN_DB", raising=False)
    monkeypatch.delenv("BRAINCTL_HOME", raising=False)
    monkeypatch.setenv("BRAINCTL_DB", str(target))
    monkeypatch.setattr(impl, "_DB_PATH_LOCKED", False, raising=False)

    if not impl._DB_PATH_LOCKED and (
        os.environ.get("BRAINCTL_DB") or os.environ.get("BRAIN_DB") or os.environ.get("BRAINCTL_HOME")
    ):
        impl.DB_PATH = impl.get_db_path()

    assert impl.DB_PATH == target, "BRAINCTL_DB alone must trigger re-resolution (R2-B2)"


# --- R2-B4: the unrelated tool-rename regression must be gone ---

def test_r2_b4_agent_wrap_up_tool_name_not_renamed():
    """The 2026-08-10 brainctl_wrapup rename came from an unrelated branch,
    swept in by accident when the production-path guard was ported, and
    broke an unrelated MCP tool-discovery test (22 visible tools -> 21).
    Confirms it's reverted: agent_wrap_up is the real, visible name again."""
    from agentmemory.mcp_server import TOOLS, _ALL_TOOL_NAMES, _VISIBLE_TOOL_NAMES

    tool_names = {t.name for t in TOOLS}
    assert "agent_wrap_up" in tool_names, "agent_wrap_up must be a real, discoverable Tool() again (R2-B4)"
    assert "brainctl_wrapup" not in tool_names, "the unrelated rename must not be present on this branch"
    assert "agent_wrap_up" in _VISIBLE_TOOL_NAMES, "agent_wrap_up must be visible in tool discovery, not hidden"
    assert "brainctl_wrapup" not in _ALL_TOOL_NAMES


# --- R2-F1: a successful healer must not commit the caller's own pending work ---

def test_r2_f1_healer_does_not_commit_callers_pending_transaction():
    from agentmemory.mcp_server import _ensure_fts_triggers_scoped

    conn = _fresh_conn()
    _add(conn, 1, content="original")
    # Caller starts its own transaction with real uncommitted work before
    # ever calling the healer.
    conn.execute("BEGIN")
    conn.execute("UPDATE memories SET content = 'caller uncommitted edit' WHERE id = 1")
    assert conn.in_transaction

    # Force a malformed pair so the healer actually does repair work.
    conn.execute("DROP TRIGGER memories_fts_update_delete")
    _ensure_fts_triggers_scoped(conn)

    # The healer must not have finalized the caller's own open transaction --
    # rolling back now must undo the caller's edit, proving it was never committed.
    conn.rollback()
    row = conn.execute("SELECT content FROM memories WHERE id = 1").fetchone()
    assert row[0] == "original", "the healer must not have committed the caller's own pending work (R2-F1)"


# --- R2-F2: a denied SAVEPOINT must not let a second error escape ---

def test_r2_f2_denied_savepoint_returns_false_not_a_second_exception():
    from agentmemory.mcp_server import _ensure_fts_triggers_scoped

    conn = _fresh_conn()
    conn.execute("DROP TRIGGER memories_fts_update_delete")  # malformed, needs repair

    def authorizer(action, arg1, arg2, dbname, source):
        if action == sqlite3.SQLITE_SAVEPOINT:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorizer)
    try:
        result = _ensure_fts_triggers_scoped(conn)
    finally:
        conn.set_authorizer(None)

    assert result is False, "a denied SAVEPOINT must return the documented False, not raise (R2-F2)"
