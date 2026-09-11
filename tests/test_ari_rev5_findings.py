"""Regression tests for Ari's independent adversarial REV5 findings
(2026-09-10), against commit 28aed0b.

FINDINGS_REV5.md: F:\\GPT Codex\\reviews\\brainctl-fts-fix-2026-09-10\\FINDINGS_REV5.md
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "src" / "agentmemory" / "db" / "init_schema.sql").read_text(encoding="utf-8")


def _write_db(db_path, eligible_present_in_fts: bool, content: str = "findable content"):
    now = "2026-09-10T12:00:00"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
        "VALUES ('t','t','test','active',?,?)", (now, now),
    )
    conn.execute(
        "INSERT INTO memories (id, content, category, tags, indexed, agent_id, created_at, updated_at) "
        "VALUES (1,?,'general','',1,'t',?,?)", (content, now, now),
    )
    conn.commit()
    if not eligible_present_in_fts:
        conn.execute("DELETE FROM memories_fts")
        conn.commit()
    return conn


# --- R5-B1: a restored/copied database carrying the same stamp must not be trusted forever ---

def _restored_db_with_phantom_membership(db_path, stamp):
    """Build a "restored" database at db_path carrying `stamp` forward, with
    two real eligible rows (ids 1 and 4) but a raw FTS index whose actual
    membership is wrong -- phantom entries at ids 2 and 3 instead. Same
    COUNT (2) and same SUM (1+4 == 2+3 == 5) as a healthy {1,4}/{1,4} state,
    so the cheap (COUNT, SUM) fingerprint genuinely can't tell these apart --
    only the full id-SET comparison inside _ensure_fts_index_consistent can."""
    conn = _write_db(db_path, eligible_present_in_fts=False)
    conn.execute(
        "INSERT INTO memories (id, content, category, tags, indexed, agent_id, created_at, updated_at) "
        "VALUES (4,'restoredrealcontent','general','',1,'t','2026-09-10T12:00:00','2026-09-10T12:00:00')"
    )
    # The schema's own memories_fts_insert trigger fires on that INSERT
    # (indexed=1) and correctly adds rowid 4 -- undo it with FTS5's
    # documented external-content 'delete' command (exact old column values
    # required to locate the entry) so the index starts genuinely empty
    # before the phantom construction below, rather than accidentally
    # already containing one real row.
    conn.execute(
        "INSERT INTO memories_fts(memories_fts, rowid, content, category, tags) "
        "VALUES ('delete', 4, 'restoredrealcontent', 'general', '')"
    )
    for phantom_id, phantom_text in ((2, "phantomtwo"), (3, "phantomthree")):
        conn.execute(
            "INSERT INTO memories_fts(rowid, content, category, tags) VALUES (?, ?, 'general', '')",
            (phantom_id, phantom_text),
        )
    conn.execute(
        "INSERT OR REPLACE INTO workspace_config (key, value) VALUES ('_db_instance_id', ?)", (stamp,)
    )
    conn.commit()
    return conn


def test_r5_b1_stamp_collision_self_heals_after_ttl(tmp_path, monkeypatch):
    """Ari's finding: the R3-B1 stamp fix trusts a stamp forever, but a
    restored backup or cloned file carries the SAME stamp forward -- it's
    the same bytes. No passive signal can tell "still the live database"
    apart from "an old snapshot of it, restored later."

    R8 note (Ari's independent REV8 audit, 2026-09-11): an earlier version
    of this test tried to assert that a genuine file-level restore
    (unlink + fresh write at the same path) is detected IMMEDIATELY via a
    file mtime/size "was this file touched" trigger layered into
    _cold_start_check_once. Measured directly rather than assumed: on this
    schema, that guarantee does NOT hold -- init_schema.sql pre-allocates
    ~587 pages regardless of content, so file size is provably identical
    across small databases sharing it, and mtime has ~2-second granularity
    on this filesystem, so two real writes within that window collide too.
    The mtime/size layer is still real and still helps in the common case
    (see _cold_start_check_once's own docstring for the honest accounting
    of what it does and doesn't cover), but asserting it as a guarantee in
    a test would assert something false. The real, deterministic guarantee
    for this specific same-count-same-sum residual is the TTL bound below,
    plus the explicit invalidate_fts_check_cache() hook (see the dedicated
    test for that) for callers who actually know a restore just happened.
    Forces the file-fingerprint collision explicitly (rather than relying
    on it happening to occur, which is itself nondeterministic) by
    overwriting the cached entry's stored file fingerprint to match the
    CURRENT file's real stat -- isolates this one path deterministically.
    TTL expiry is simulated by directly backdating the recorded check
    timestamp, not by sleeping -- deterministic, no real elapsed time
    required either."""
    import os as _os
    import agentmemory.mcp_server as srv

    if not hasattr(srv, "_cold_start_check_once") or not hasattr(srv, "_db_instance_id"):
        pytest.skip("_cold_start_check_once/_db_instance_id don't exist on this commit -- new functionality")

    srv._FTS_REBUILD_CHECKED_PATHS.clear()

    db_path = tmp_path / "shared-name.db"

    conn1 = _write_db(db_path, eligible_present_in_fts=True)
    assert srv._cold_start_check_once(conn1, db_path) is True
    stamp = conn1.execute(
        "SELECT value FROM workspace_config WHERE key = '_db_instance_id'"
    ).fetchone()[0]
    conn1.close()

    db_path.unlink()
    conn2 = _restored_db_with_phantom_membership(db_path, stamp)

    # Force the "file not touched" case: overwrite the cached entry so its
    # stored file fingerprint matches the CURRENT (post-restore) file's real
    # stat exactly, the one condition the mtime/size trigger can't detect by
    # construction (see the R8 docstring). Everything else about the cache
    # entry (same db_key, since the stamp collides) is left as the real
    # code wrote it.
    _stat = _os.stat(db_path)
    current_fp = (_stat.st_mtime_ns, _stat.st_size)
    for key, (last_checked, _old_fp) in list(srv._FTS_REBUILD_CHECKED_PATHS.items()):
        srv._FTS_REBUILD_CHECKED_PATHS[key] = (last_checked, current_fp)

    # Immediately after, with the file-touch trigger neutralized: still
    # within the TTL window, so the stamp collision is (correctly, by
    # design) still trusted -- this isn't the bug, it's the intentional bound.
    assert srv._cold_start_check_once(conn2, db_path) is False
    assert not conn2.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'restoredrealcontent'"
    ).fetchall(), "sanity: real content must not be findable before repair"

    # Past the TTL, simulated deterministically: the same stamp collision
    # must now self-heal, and the repair must be REAL.
    for key, (last_checked, fp) in list(srv._FTS_REBUILD_CHECKED_PATHS.items()):
        srv._FTS_REBUILD_CHECKED_PATHS[key] = (last_checked - (srv._FTS_REBUILD_CHECK_TTL_SECONDS + 1), fp)
    ran = srv._cold_start_check_once(conn2, db_path)
    assert ran is True, (
        "a stamp collision from a restored/copied database must self-heal "
        "once the TTL window has passed (R5-B1)"
    )

    indexed_ids = {
        r[0] for r in conn2.execute("SELECT rowid FROM memories_fts_docsize").fetchall()
    }
    assert indexed_ids == {1, 4}, (
        "after the TTL-triggered check, the raw FTS index must actually "
        f"contain the real eligible ids, not just have been touched -- got {indexed_ids} (R5-B1/F1)"
    )
    assert conn2.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'restoredrealcontent'"
    ).fetchall(), "the real restored content must be findable after repair, not just present in the id set"
    assert not conn2.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'phantomtwo'"
    ).fetchall(), "phantom pre-repair content must be gone after repair, not left alongside the real rows"


def test_invalidate_fts_check_cache_closes_the_passive_detection_gap(tmp_path, monkeypatch):
    """Real, verified case where mtime/size CANNOT detect a restore at all:
    same real ids (1, 4) present in both the original and the "restored"
    database, but the restored one's raw FTS index carries stale text at
    those same ids (real content says wantedrestoretoken4, raw index still
    says obsoleteindextoken4). Measured directly (see
    _cold_start_check_once's R8 docstring): on this schema, file size is
    identical across any two small databases regardless of content (the
    schema itself pre-allocates ~587 pages, dwarfing a handful of content
    rows), and mtime has ~2-second granularity on this filesystem -- so a
    restore landing inside that window is genuinely invisible to the
    passive layer, not a contrived worst case. invalidate_fts_check_cache()
    exists for exactly this: called at the moment a restore is KNOWN to
    have happened (which passive inspection can't always tell), it forces
    the next real search to be correct unconditionally."""
    import agentmemory.mcp_server as srv

    if not hasattr(srv, "invalidate_fts_check_cache"):
        pytest.skip("invalidate_fts_check_cache doesn't exist on this commit -- new functionality")

    srv._FTS_REBUILD_CHECKED_PATHS.clear()
    db_path = tmp_path / "shared-name.db"
    monkeypatch.setattr(srv, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(srv, "_DB_PATH_LOCKED", True, raising=False)

    conn1 = _write_db(db_path, eligible_present_in_fts=True)
    conn1.close()
    first = srv.tool_memory_search(agent_id="t", query="findable", benchmark=True)
    assert first["ok"] and first["memories"], "sanity: original content must be findable"

    # NOTE: sqlite3's own context-manager protocol only commits/rolls back
    # the transaction on exit, it does NOT close the connection -- leaving
    # this open would hold a Windows file lock and make the unlink() below
    # fail. Close explicitly.
    _c = sqlite3.connect(db_path)
    stamp = _c.execute(
        "SELECT value FROM workspace_config WHERE key = '_db_instance_id'"
    ).fetchone()[0]
    _c.close()

    db_path.unlink()
    conn2 = _write_db(db_path, eligible_present_in_fts=False)
    conn2.execute(
        "INSERT INTO memories (id, content, category, tags, indexed, agent_id, created_at, updated_at) "
        "VALUES (4,'wantedrestoretoken4','general','',1,'t','2026-09-10T12:00:00','2026-09-10T12:00:00')"
    )
    # Real ids 1 and 4 both present, but the raw FTS index carries STALE
    # text at those exact same ids -- the case an id/count/sum fingerprint
    # can never see, matching Ari's own REV8 "same-ids-stale-tokens" repro.
    for stale_id, stale_text in ((1, "obsoleteindextoken1"), (4, "obsoleteindextoken4")):
        conn2.execute(
            "INSERT INTO memories_fts(rowid, content, category, tags) VALUES (?, ?, 'general', '')",
            (stale_id, stale_text),
        )
    conn2.execute(
        "INSERT OR REPLACE INTO workspace_config (key, value) VALUES ('_db_instance_id', ?)", (stamp,)
    )
    conn2.commit()
    conn2.close()

    # The explicit call this test exists to prove: a caller who KNOWS a
    # restore just happened tells the cache so, unconditionally.
    srv.invalidate_fts_check_cache(db_path)

    wanted = srv.tool_memory_search(agent_id="t", query="wantedrestoretoken4", benchmark=True)
    stale = srv.tool_memory_search(agent_id="t", query="obsoleteindextoken4", benchmark=True)
    assert wanted["ok"] and 4 in [m["id"] for m in wanted["memories"]], (
        "after explicit invalidation, the real restored content must be "
        "findable on the very next search -- not silently missing"
    )
    assert not stale["memories"], (
        "after explicit invalidation, the obsolete pre-restore token must "
        "not return content that no longer matches it"
    )


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
