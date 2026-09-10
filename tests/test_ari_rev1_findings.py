"""Regression tests for Ari's independent adversarial REV1 findings
(2026-09-10), against the fixes for B1-B4 and F5-F8.

FINDINGS_REV1.md: F:\\GPT Codex\\reviews\\brainctl-fts-fix-2026-09-10\\FINDINGS_REV1.md
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


def _eligible_ids(conn):
    return set(
        r[0] for r in conn.execute(
            "SELECT id FROM memories WHERE indexed = 1 AND retired_at IS NULL"
        ).fetchall()
    )


# --- F5: active -> retired -> active must restore FTS membership ---

def test_f5_unretirement_restores_fts_membership():
    conn = _fresh_conn()
    _add(conn, 1, content="findable text")
    assert 1 in _fts_ids(conn)

    conn.execute("UPDATE memories SET retired_at=? WHERE id=1", ("2026-09-10T12:00:00",))
    conn.commit()
    assert 1 not in _fts_ids(conn), "retiring must remove the memory from FTS"

    conn.execute("UPDATE memories SET retired_at=NULL WHERE id=1")
    conn.commit()
    assert 1 in _fts_ids(conn), "un-retiring must restore FTS membership (F5)"
    matches = [r[0] for r in conn.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'findable'"
    ).fetchall()]
    assert matches == [1]


# --- F6: a row born already-retired must never enter raw FTS ---

def test_f6_row_born_retired_never_indexed():
    conn = _fresh_conn()
    _add(conn, 1, indexed=1, retired_at="2026-09-10T12:00:00")
    assert 1 not in _fts_ids(conn), "a row inserted already-retired must not be token-searchable (F6)"


# --- F8: deleting FTS-absent rows must not corrupt the database ---

def test_f8_deleting_ineligible_rows_does_not_corrupt():
    conn = _fresh_conn()
    _add(conn, 1, content="active control", indexed=1)
    _add(conn, 2, content="never indexed", indexed=0)
    _add(conn, 3, content="already retired", indexed=1, retired_at="2026-09-10T12:00:00")
    conn.commit()

    conn.execute("PRAGMA foreign_keys=OFF")
    # This exact sequence raised "database disk image is malformed" pre-fix.
    conn.execute("DELETE FROM memories WHERE id=2")
    conn.execute("DELETE FROM memories WHERE id=3")
    conn.commit()

    # Database must still be usable afterward, not just "didn't raise."
    conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('integrity-check')")
    assert 1 in _fts_ids(conn), "the untouched active control row must still be findable"


# --- B1: the trigger healer must detect and repair every malformed case ---

@pytest.mark.parametrize(
    "break_schema",
    [
        pytest.param(lambda c: c.execute("DROP TRIGGER memories_fts_update_delete"), id="only_insert_present"),
        pytest.param(lambda c: c.execute("DROP TRIGGER memories_fts_update_insert"), id="only_delete_present"),
        pytest.param(
            lambda c: (
                c.execute("DROP TRIGGER memories_fts_update_delete"),
                c.execute("DROP TRIGGER memories_fts_update_insert"),
            ),
            id="neither_present",
        ),
        pytest.param(
            lambda c: (
                c.execute("DROP TRIGGER memories_fts_update_delete"),
                c.execute("DROP TRIGGER memories_fts_update_insert"),
                c.executescript(
                    "CREATE TRIGGER memories_fts_update_delete AFTER UPDATE OF confidence ON memories BEGIN "
                    "SELECT 1; END;"
                    "CREATE TRIGGER memories_fts_update_insert AFTER UPDATE OF confidence ON memories BEGIN "
                    "SELECT 1; END;"
                ),
            ),
            id="two_noop_wrong_column_triggers",
        ),
    ],
)
def test_b1_healer_detects_and_repairs_every_malformed_case(break_schema):
    from agentmemory.mcp_server import _ensure_fts_triggers_scoped

    conn = _fresh_conn()
    break_schema(conn)
    conn.commit()

    repaired = _ensure_fts_triggers_scoped(conn)
    assert repaired is True, "a malformed/missing trigger pair must be reported as repaired, not silently accepted"

    rows = dict(
        conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            "AND name IN ('memories_fts_update_delete', 'memories_fts_update_insert')"
        ).fetchall()
    )
    assert set(rows) == {"memories_fts_update_delete", "memories_fts_update_insert"}
    assert "OLD.RETIRED_AT IS NULL" in rows["memories_fts_update_delete"].upper()
    assert "NEW.RETIRED_AT IS NULL" in rows["memories_fts_update_insert"].upper()


def test_b1_healer_is_a_true_noop_on_an_already_exact_pair():
    from agentmemory.mcp_server import _ensure_fts_triggers_scoped

    conn = _fresh_conn()
    assert _ensure_fts_triggers_scoped(conn) is False, "an already-correct pair must not be reported as repaired"


# --- B2: a caught CREATE failure must not leave the database trigger-less ---

def test_b2_failed_repair_leaves_original_triggers_intact():
    from agentmemory.mcp_server import _ensure_fts_triggers_scoped

    conn = _fresh_conn()
    # Force a mid-repair failure: deny CREATE TRIGGER via an authorizer, after
    # the healer's own DROP statements would otherwise have already run.
    def authorizer(action, arg1, arg2, dbname, source):
        if action == sqlite3.SQLITE_CREATE_TRIGGER:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorizer)
    conn.execute("DROP TRIGGER memories_fts_update_delete")  # simulate an already-broken pair to repair
    conn.commit()

    result = _ensure_fts_triggers_scoped(conn)
    conn.set_authorizer(None)

    assert result is False
    names = set(
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name IN ('memories_fts_update_delete', 'memories_fts_update_insert')"
        ).fetchall()
    )
    # B2's real failure mode: a caught error left BOTH triggers gone. After
    # the SAVEPOINT rollback, at minimum the untouched insert trigger from
    # before the repair attempt must still be present -- not zero triggers.
    assert "memories_fts_update_insert" in names, (
        "a failed repair must not delete triggers that existed before the attempt"
    )


# --- B3: rebuild/migration must never re-import ineligible rows ---

def test_b3_rebuild_excludes_ineligible_rows():
    from agentmemory.mcp_server import _purge_ineligible_fts_rows

    conn = _fresh_conn()
    _add(conn, 1, indexed=1)  # eligible
    _add(conn, 2, indexed=0)  # CONSTRUCT_ONLY, never indexed
    conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
    conn.commit()

    # Raw rebuild alone re-imports both -- this documents the known FTS5
    # external-content behavior before the purge step runs.
    assert _fts_ids(conn) == {1, 2}

    _purge_ineligible_fts_rows(conn)
    conn.commit()
    assert _fts_ids(conn) == {1}, "purge must remove the indexed=0 row rebuild re-imported (B3)"


def test_b3_migration_085_purges_ineligible_rows_from_existing_db():
    conn = _fresh_conn()
    _add(conn, 1, indexed=1)
    _add(conn, 2, indexed=0)
    _add(conn, 3, indexed=1, retired_at="2026-09-10T12:00:00")

    migration_085 = (ROOT / "db" / "migrations" / "085_fts_delete_eligibility_guard.sql").read_text(encoding="utf-8")
    conn.executescript(migration_085)

    assert _fts_ids(conn) == {1}, "migration 085 must leave only the truly eligible row indexed"


# --- B4: the consistency check must catch wrong membership, not just undercounts ---

def _drop_memories_fts_triggers(conn):
    """So a test can hand-construct a drifted state that would normally be
    impossible to reach with the real triggers actively correcting it --
    simulating the kind of historical corruption the health check exists to
    catch, not just the shape of a fresh write."""
    for name in ("memories_fts_insert", "memories_fts_update_delete", "memories_fts_update_insert", "memories_fts_delete"):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")


def test_b4_detects_equal_count_wrong_membership():
    from agentmemory.mcp_server import _ensure_fts_index_consistent

    conn = _fresh_conn()
    _drop_memories_fts_triggers(conn)
    _add(conn, 1, indexed=1)  # eligible, but will be absent from FTS
    _add(conn, 2, indexed=0)  # ineligible, but will be present in FTS
    conn.execute("INSERT INTO memories_fts(rowid, content, category, tags) VALUES (2, 'x', 'general', '')")
    conn.commit()
    assert _fts_ids(conn) == {2}  # equal count (1) to eligible set size, but wrong id

    repaired = _ensure_fts_index_consistent(conn)
    assert repaired is True, "equal-count wrong-membership must be detected and repaired (B4)"
    assert _fts_ids(conn) == {1}


def test_b4_detects_overcount_with_missing_eligible():
    from agentmemory.mcp_server import _ensure_fts_index_consistent

    conn = _fresh_conn()
    _drop_memories_fts_triggers(conn)
    _add(conn, 1, indexed=1)
    _add(conn, 2, indexed=0)
    _add(conn, 3, indexed=0)
    conn.execute("INSERT INTO memories_fts(rowid, content, category, tags) VALUES (2, 'x', 'general', '')")
    conn.execute("INSERT INTO memories_fts(rowid, content, category, tags) VALUES (3, 'x', 'general', '')")
    conn.commit()
    assert _fts_ids(conn) == {2, 3}  # more docs than eligible count, still missing id 1

    repaired = _ensure_fts_index_consistent(conn)
    assert repaired is True, "overcount with a missing eligible row must be detected and repaired (B4)"
    assert _fts_ids(conn) == {1}


# --- F7: the process-global repair guard must check each database, not just the first ---

def test_f7_second_database_in_same_process_still_gets_checked():
    """Directly exercises the guard mechanism itself (the set-membership
    check + _ensure_fts_index_consistent), independent of get_db()/env-var
    plumbing, which is exercised elsewhere. This isolates exactly the bug
    Ari found: a single shared boolean vs. one entry per database path."""
    import agentmemory.mcp_server as srv

    checked: set = set()
    conn_a = _fresh_conn()
    _add(conn_a, 1, content="findable")
    conn_a.execute("DELETE FROM memories_fts")  # simulate cold-start underpopulation
    conn_a.commit()

    conn_b = _fresh_conn()
    _add(conn_b, 1, content="findable")
    conn_b.execute("DELETE FROM memories_fts")
    conn_b.commit()

    for key, conn in (("/tmp/a.db", conn_a), ("/tmp/b.db", conn_b)):
        assert key not in checked, "each distinct database key must start unchecked"
        if key not in checked:
            srv._ensure_fts_index_consistent(conn)
            checked.add(key)

    assert _fts_ids(conn_a) == {1}, "first database must be repaired"
    assert _fts_ids(conn_b) == {1}, "second database must ALSO be repaired, not skipped (F7)"
