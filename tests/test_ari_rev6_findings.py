"""Regression tests for Ari's independent adversarial REV6 findings
(2026-09-10), against commit 886c72a.

FINDINGS_REV6.md: F:\\GPT Codex\\reviews\\brainctl-fts-fix-2026-09-10\\FINDINGS_REV6.md
Ari's own fresh reproductions: test_adversarial_fts_rev6.py, and the
restore reproducer in test_adversarial_fts_rev5.py, same folder.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "src" / "agentmemory" / "db" / "init_schema.sql").read_text(encoding="utf-8")


def _full_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO agents(id, display_name, agent_type) "
        "VALUES('tester', 'Tester', 'assistant')"
    )
    conn.commit()
    return conn


def _add(conn: sqlite3.Connection, token: str, *, indexed: int = 1) -> int:
    cur = conn.execute(
        "INSERT INTO memories(agent_id, category, content, tags, indexed, created_at, updated_at) "
        "VALUES('tester', 'lesson', ?, '', ?, "
        "strftime('%Y-%m-%dT%H:%M:%S','now'), strftime('%Y-%m-%dT%H:%M:%S','now'))",
        (token, indexed),
    )
    conn.commit()
    return int(cur.lastrowid)


# --- R6-B1: a temporary env override must revert when removed, in all three modules ---

@pytest.mark.parametrize(
    "module_name", ["agentmemory._impl", "agentmemory.hippocampus", "agentmemory.mcp_server"]
)
def test_r6_b1_temporary_env_override_reverts_when_removed(module_name, tmp_path, monkeypatch):
    """Ari's finding: scheduler.py's real run_once() pattern temporarily sets
    BRAIN_DB, calls get_db(), then removes it in a finally block, expecting
    the next call to fall back to the ambient default. The R5-B2 gate
    required a relevant env var to be CURRENTLY set to re-derive at all, so
    removing the override left DB_PATH pinned to the temporary path forever.
    This calls the real get_db() through exactly that set/call/remove/call
    sequence, in each of the three path-owning modules."""
    import importlib

    from agentmemory import paths

    module = importlib.import_module(module_name)

    if not hasattr(module, "_DB_PATH_DEFAULT"):
        pytest.skip("_DB_PATH_DEFAULT doesn't exist on this commit -- new functionality")

    home = tmp_path / f"{module_name.rsplit('.', 1)[-1]}-home"
    default = home / "db" / "brain.db"
    temporary = tmp_path / f"{module_name.rsplit('.', 1)[-1]}-temporary.db"
    default.parent.mkdir(parents=True)
    _full_db(default).close()
    _full_db(temporary).close()

    for name in ("BRAINCTL_DB", "BRAIN_DB", "BRAINCTL_HOME"):
        monkeypatch.delenv(name, raising=False)
    # get_db_path()'s own fallback (when no env vars are set) is
    # get_brain_home()/db/brain.db -- patching _DEFAULT_HOME is what makes
    # that real fallback resolve to THIS test's disposable "default", rather
    # than the actual user home directory.
    monkeypatch.setattr(paths, "_DEFAULT_HOME", home, raising=False)
    monkeypatch.setattr(module, "_DB_PATH_LOCKED", False, raising=False)
    monkeypatch.setattr(module, "DB_PATH", default, raising=False)
    monkeypatch.setattr(module, "_DB_PATH_DEFAULT", default, raising=False)

    monkeypatch.setenv("BRAIN_DB", str(temporary))
    conn = module.get_db()
    conn.close()
    assert module.DB_PATH == temporary, "the temporary override must take effect"

    monkeypatch.delenv("BRAIN_DB")
    conn = module.get_db()
    conn.close()
    assert module.DB_PATH == default, (
        "removing a temporary environment override must restore the ambient "
        "default instead of leaving the process pinned to the temporary "
        "database (R6-B1)"
    )


# --- R6-B2: a database restored with the same stamp must not silently miss content ---

def test_r6_b2_restored_db_with_same_stamp_is_found_immediately(tmp_path, monkeypatch):
    """Ari's finding: the R5-B1 TTL bounds the stamp-collision staleness, but
    a database restored with a wiped/underpopulated FTS index during that
    window still silently returns no results for real content -- up to 5
    real minutes of a normal restore looking like data loss. This calls the
    real tool_memory_search MCP tool (not the lower-level helpers) through
    Ari's exact repro: stamp+cache a live db, then swap in a genuinely
    different db carrying the SAME stamp and a wiped FTS index at the same
    path, and confirm the very next search still finds the real content --
    not after waiting out the TTL."""
    import agentmemory.mcp_server as srv

    if not hasattr(srv, "_cold_start_check_once"):
        pytest.skip("_cold_start_check_once doesn't exist on this commit -- new functionality")

    live = tmp_path / "live.db"
    first = _full_db(live)
    first_id = _add(first, "stampedfirstunique")
    first.close()

    monkeypatch.setattr(srv, "DB_PATH", live, raising=False)
    monkeypatch.setattr(srv, "_DB_PATH_LOCKED", True, raising=False)
    srv._FTS_REBUILD_CHECKED_PATHS.clear()

    first_result = srv.tool_memory_search(agent_id="tester", query="stampedfirstunique", benchmark=True)
    assert first_id in [m["id"] for m in first_result["memories"]]

    conn = sqlite3.connect(live)
    stamp = conn.execute("SELECT value FROM workspace_config WHERE key='_db_instance_id'").fetchone()[0]
    conn.close()

    restored_path = tmp_path / "restored.db"
    second = _full_db(restored_path)
    second_id = _add(second, "stampedotherunique")
    second.execute(
        "INSERT OR REPLACE INTO workspace_config(key,value) VALUES('_db_instance_id',?)", (stamp,)
    )
    second.execute("INSERT INTO memories_fts(memories_fts) VALUES('delete-all')")
    second.commit()
    second.close()

    live.unlink()
    restored_path.replace(live)

    second_result = srv.tool_memory_search(agent_id="tester", query="stampedotherunique", benchmark=True)
    assert second_id in [m["id"] for m in second_result["memories"]], (
        "a database restored with the same stamp but different, wiped-FTS "
        "content must be found on the very next search, not silently "
        "missing for up to the TTL window (R6-B2)"
    )
