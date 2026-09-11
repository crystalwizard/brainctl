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
    """Ari's REV2 correction: inserting row 3 already-retired means the OLD
    F6 bug (insert trigger checks indexed=1 but not retired_at) puts it into
    FTS anyway on pre-fix code -- so deleting it there doesn't reproduce
    "deleting a row absent from FTS" at all, invalidating the red-before
    proof. Fixed by inserting it ACTIVE first (genuinely indexed on both old
    and new code), then retiring it via UPDATE -- the update-delete trigger
    correctly removes it from FTS on a *first* retirement on old code too
    (that specific transition isn't F5's bug), so it's genuinely absent
    before the corruption-probe deletes, on both code versions."""
    conn = _fresh_conn()
    _add(conn, 1, content="active control", indexed=1)
    _add(conn, 2, content="never indexed", indexed=0)
    _add(conn, 3, content="will be retired", indexed=1)
    conn.commit()
    assert 3 in _fts_ids(conn), "row 3 must start genuinely indexed"

    conn.execute("UPDATE memories SET retired_at=? WHERE id=3", ("2026-09-10T12:00:00",))
    conn.commit()
    assert 3 not in _fts_ids(conn), "retiring must have actually removed it from FTS before the probe"

    conn.execute("PRAGMA foreign_keys=OFF")
    # This exact sequence raised "database disk image is malformed" pre-fix:
    # deleting two rows in one transaction, neither present in FTS.
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
    # Ari's REV2 correction: a missing-trigger setup never reaches the old
    # destructive repair path at all -- the OLD detector's `not rows or all(...)`
    # check already returns False (no-op) for any missing-trigger case, same
    # bug as B1. To actually exercise the repair *action* (which is what B2 is
    # about), start from a legacy UNSCOPED pair -- present, but with no
    # "AFTER UPDATE OF" column clause -- which the old substring check does
    # NOT recognize as already-correct, so it proceeds into repair. This
    # matches Ari's own reproduction exactly ("valid legacy unscoped
    # delete/insert trigger pair").
    conn.execute("DROP TRIGGER memories_fts_update_delete")
    conn.execute("DROP TRIGGER memories_fts_update_insert")
    conn.executescript(
        "CREATE TRIGGER memories_fts_update_delete AFTER UPDATE ON memories WHEN old.indexed = 1 BEGIN "
        "INSERT INTO memories_fts(memories_fts, rowid, content, category, tags) "
        "VALUES ('delete', old.id, old.content, old.category, old.tags); END;"
        "CREATE TRIGGER memories_fts_update_insert AFTER UPDATE ON memories WHEN new.indexed = 1 BEGIN "
        "INSERT INTO memories_fts(rowid, content, category, tags) "
        "VALUES (new.id, new.content, new.category, new.tags); END;"
    )
    conn.commit()

    # Force a mid-repair failure: deny CREATE TRIGGER via an authorizer, after
    # the healer's own DROP statements would otherwise have already run.
    def authorizer(action, arg1, arg2, dbname, source):
        if action == sqlite3.SQLITE_CREATE_TRIGGER:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorizer)
    result = _ensure_fts_triggers_scoped(conn)
    conn.set_authorizer(None)

    assert result is False
    names = set(
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name IN ('memories_fts_update_delete', 'memories_fts_update_insert')"
        ).fetchall()
    )
    # B2's real failure mode: a caught error left BOTH triggers gone (the old
    # code's read-back trigger set was empty). Both original (legacy, but
    # present and working) triggers must survive a failed repair attempt.
    assert names == {"memories_fts_update_delete", "memories_fts_update_insert"}, (
        "a failed repair must not delete triggers that existed before the attempt"
    )


# --- B3: rebuild/migration must never re-import ineligible rows ---

def test_b3_rebuild_excludes_ineligible_rows():
    """Ari's REV2 correction: importing the brand-new _purge_ineligible_fts_rows
    directly means this errors with ImportError on the pre-fix parent (the
    symbol doesn't exist yet) rather than failing by assertion. Rewritten to
    go through _ensure_fts_index_consistent instead -- that function exists
    on BOTH versions, just with weaker (pre-fix) or corrected (post-fix)
    behavior, so this now has real red-before/green-after shape."""
    from agentmemory.mcp_server import _ensure_fts_index_consistent

    conn = _fresh_conn()
    _drop_memories_fts_triggers(conn)
    _add(conn, 1, indexed=1)  # eligible
    _add(conn, 2, indexed=0)  # CONSTRUCT_ONLY, never indexed
    conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
    conn.commit()
    # Raw rebuild alone re-imports both -- documents the known FTS5
    # external-content behavior, real on both code versions.
    assert _fts_ids(conn) == {1, 2}

    _ensure_fts_index_consistent(conn)
    assert _fts_ids(conn) == {1}, "the consistency check must purge the indexed=0 row rebuild re-imported (B3)"


def test_b3_migration_085_purges_ineligible_rows_from_existing_db():
    """Migration 085 is itself a brand-new file with no pre-fix counterpart --
    there is no meaningful "old behavior" to reproduce, so this intentionally
    isn't a red-before/green-after test like the others. Skips cleanly with
    a clear reason rather than crashing if transplanted onto a commit that
    doesn't have the file yet."""
    migration_085_path = ROOT / "db" / "migrations" / "085_fts_delete_eligibility_guard.sql"
    if not migration_085_path.exists():
        pytest.skip("migration 085 doesn't exist on this commit -- new functionality, not a regression to reproduce")

    conn = _fresh_conn()
    _add(conn, 1, indexed=1)
    _add(conn, 2, indexed=0)
    _add(conn, 3, indexed=1, retired_at="2026-09-10T12:00:00")

    migration_085 = migration_085_path.read_text(encoding="utf-8")
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


# --- F7/R2-F3: the process-global repair guard must check each real database ---

def test_f7_second_database_in_same_process_still_gets_checked(tmp_path):
    """Calls the REAL guard function (_cold_start_check_once), against the
    REAL module-level _FTS_REBUILD_CHECKED_PATHS set -- Ari's REV2 review
    found the original version of this test built its own throwaway local
    set instead and never touched either. This uses real files on disk so
    os.stat() (part of the real key) behaves exactly as it does in
    production, not an in-memory stand-in. Like migration 085, the
    per-path-set replacement is itself new -- the pre-fix parent has no
    counterpart to reproduce a "same behavior, wrong result" regression
    against, so this skips cleanly there rather than crashing."""
    import agentmemory.mcp_server as srv

    if not hasattr(srv, "_cold_start_check_once") or not hasattr(srv, "_FTS_REBUILD_CHECKED_PATHS"):
        pytest.skip("_cold_start_check_once/_FTS_REBUILD_CHECKED_PATHS don't exist on this commit -- new functionality")

    srv._FTS_REBUILD_CHECKED_PATHS.clear()

    def _make_underpopulated_db(path):
        conn = sqlite3.connect(str(path))
        conn.executescript(SCHEMA)
        now = "2026-09-10T12:00:00"
        conn.execute(
            "INSERT INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
            "VALUES ('t','t','test','active',?,?)", (now, now),
        )
        conn.execute(
            "INSERT INTO memories (id, content, category, tags, indexed, agent_id, created_at, updated_at) "
            "VALUES (1,'findable content','general','',1,'t',?,?)", (now, now),
        )
        conn.commit()
        conn.execute("DELETE FROM memories_fts")  # simulate cold-start underpopulation
        conn.commit()
        return conn

    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    conn_a = _make_underpopulated_db(db_a)
    conn_b = _make_underpopulated_db(db_b)

    ran_a = srv._cold_start_check_once(conn_a, db_a)
    ran_b = srv._cold_start_check_once(conn_b, db_b)

    assert ran_a is True and ran_b is True, "a distinct database path must always get its own check (F7)"
    assert set(r[0] for r in conn_a.execute("SELECT rowid FROM memories_fts_docsize").fetchall()) == {1}
    assert set(r[0] for r in conn_b.execute("SELECT rowid FROM memories_fts_docsize").fetchall()) == {1}

    # Re-checking the exact same (path, file state) again must be a no-op.
    ran_a_again = srv._cold_start_check_once(conn_a, db_a)
    assert ran_a_again is False, "the same database, unchanged, must not be re-checked every call"


def test_r2_f3_replacing_db_file_at_same_path_still_gets_checked(tmp_path):
    """The specific gap R2-F3 named: path alone as the cache key means
    swapping in a fresh, underpopulated database under the SAME filename
    was silently skipped because that path string was already marked
    checked. Folding file mtime+size into the key must catch this. Same as
    F7's test: new functionality, skips cleanly on a commit that predates it
    rather than crashing."""
    import agentmemory.mcp_server as srv
    import time

    if not hasattr(srv, "_cold_start_check_once") or not hasattr(srv, "_FTS_REBUILD_CHECKED_PATHS"):
        pytest.skip("_cold_start_check_once/_FTS_REBUILD_CHECKED_PATHS don't exist on this commit -- new functionality")

    srv._FTS_REBUILD_CHECKED_PATHS.clear()
    db_path = tmp_path / "shared-name.db"
    now = "2026-09-10T12:00:00"

    def _write_db(eligible_present_in_fts: bool):
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

    conn1 = _write_db(eligible_present_in_fts=True)
    assert srv._cold_start_check_once(conn1, db_path) is True  # first check of this exact file
    conn1.close()

    # Measured directly on this filesystem: mtime_ns advances in 2-second
    # jumps (not 1s), and the two db files here land on the exact same size
    # (identical schema + row count), so a 1.1s gap collides with the same
    # mtime bucket ~40% of the time -- that's what was flaking, not "load".
    # 2.5s reliably crosses a 2s granularity boundary.
    time.sleep(2.5)
    db_path.unlink()
    conn2 = _write_db(eligible_present_in_fts=False)  # a genuinely different, underpopulated db, same filename

    ran = srv._cold_start_check_once(conn2, db_path)
    assert ran is True, "a real file replacement at the same path must not be skipped as already-checked (R2-F3)"
    assert set(r[0] for r in conn2.execute("SELECT rowid FROM memories_fts_docsize").fetchall()) == {1}
