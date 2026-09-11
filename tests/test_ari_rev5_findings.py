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

def test_r5_b1_stamp_collision_self_heals_after_ttl(tmp_path, monkeypatch):
    """Ari's finding: the R3-B1 stamp fix trusts a stamp forever, but a
    restored backup or cloned file carries the SAME stamp forward -- it's
    the same bytes. No passive signal can tell "still the live database"
    apart from "an old snapshot of it, restored later." The real fix bounds
    the resulting staleness with a TTL rather than chasing a perfect
    identity signal.

    R7-F1 fix (Ari's independent REV7 audit, 2026-09-11): the prior version
    of this test constructed two already-*healthy* fixtures (same single
    row, present in FTS, in both) and asserted only that a check *ran*
    (`ran is True`) after the TTL. Ari proved that assertion passes even
    with `_ensure_fts_index_consistent` replaced by a no-op -- it proves
    scheduling, not repair, because there was never anything wrong to
    repair. It also depended on real `time.sleep()` against a 0.2s TTL,
    which is exactly the kind of timing assumption Ari's own F1 finding
    flagged as unstable elsewhere in this same fix.

    Fixed both problems: (1) the "restored" database now has a GENUINELY
    wrong id-membership relative to the live one -- eligible ids {1, 4} in
    `memories`, but the raw FTS shadow index carrying phantom ids {2, 3}
    instead. Count (2) and the R7-B1 (COUNT, SUM(id)) fingerprint (2, 5)
    collide exactly with the healthy case, by construction -- this is the
    one residual gap R7-B1's stronger fingerprint still can't close (see
    the R7-B1 docstring on _cold_start_check_once), which is precisely why
    the TTL backstop still needs to exist and still needs a real test. Only
    the full id-SET comparison inside `_ensure_fts_index_consistent` can
    catch it. (2) the TTL expiry is now simulated by directly backdating
    the recorded check timestamp in `_FTS_REBUILD_CHECKED_PATHS`, not by
    sleeping -- deterministic, no real elapsed time required at all."""
    import agentmemory.mcp_server as srv

    if not hasattr(srv, "_cold_start_check_once") or not hasattr(srv, "_db_instance_id"):
        pytest.skip("_cold_start_check_once/_db_instance_id don't exist on this commit -- new functionality")

    srv._FTS_REBUILD_CHECKED_PATHS.clear()

    db_path = tmp_path / "shared-name.db"

    conn1 = _write_db(db_path, eligible_present_in_fts=True)
    assert srv._cold_start_check_once(conn1, db_path) is True  # first check, stamps the instance id
    stamp = conn1.execute(
        "SELECT value FROM workspace_config WHERE key = '_db_instance_id'"
    ).fetchone()[0]
    conn1.close()

    # Simulate a restored backup: a genuinely different database that
    # happens to carry the SAME stamp forward (exactly what a real file
    # copy/restore would do -- the stamp travels with the bytes), with two
    # real eligible rows (ids 1 and 4) but a raw FTS index whose actual
    # membership is wrong -- phantom entries at ids 2 and 3 instead. Same
    # COUNT (2) and same SUM (1+4 == 2+3 == 5) as the healthy case, so
    # R7-B1's cheap fingerprint genuinely can't tell these apart; this is
    # the real residual the TTL bound exists for.
    db_path.unlink()
    conn2 = _write_db(db_path, eligible_present_in_fts=False)
    conn2.execute(
        "INSERT INTO memories (id, content, category, tags, indexed, agent_id, created_at, updated_at) "
        "VALUES (4,'restoredrealcontent','general','',1,'t','2026-09-10T12:00:00','2026-09-10T12:00:00')"
    )
    # The schema's own memories_fts_insert trigger fires on that INSERT
    # (indexed=1) and correctly adds rowid 4 -- undo it with FTS5's
    # documented external-content 'delete' command (exact old column values
    # required to locate the entry) so the index starts genuinely empty
    # before the phantom construction below, rather than accidentally
    # already containing one real row.
    conn2.execute(
        "INSERT INTO memories_fts(memories_fts, rowid, content, category, tags) "
        "VALUES ('delete', 4, 'restoredrealcontent', 'general', '')"
    )
    # Direct external-content FTS5 shadow manipulation: index phantom rowids
    # 2 and 3 (which do not exist in `memories` at all) instead of the real
    # eligible rows 1 and 4. This is the documented way to write an
    # external-content FTS5 index directly, and it's exactly what a
    # corrupted/wrongly-rebuilt raw index looks like from the check's point
    # of view -- count and id-sum both collide with truth, membership does not.
    for phantom_id, phantom_text in ((2, "phantomtwo"), (3, "phantomthree")):
        conn2.execute(
            "INSERT INTO memories_fts(rowid, content, category, tags) VALUES (?, ?, 'general', '')",
            (phantom_id, phantom_text),
        )
    conn2.execute(
        "INSERT OR REPLACE INTO workspace_config (key, value) VALUES ('_db_instance_id', ?)", (stamp,)
    )
    conn2.commit()

    # Immediately after: still within the TTL window, so the stamp
    # collision is (correctly, by design) still trusted -- this isn't the
    # bug, it's the intentional bound. The wrong content is still findable
    # under its phantom token and the real content is still missing.
    assert srv._cold_start_check_once(conn2, db_path) is False
    assert not conn2.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'restoredrealcontent'"
    ).fetchall(), "sanity: real content must not be findable before repair"
    assert conn2.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'phantomtwo'"
    ).fetchall(), "sanity: phantom content must be findable before repair"

    # Past the TTL, simulated deterministically: the same stamp collision
    # must now self-heal, and the repair must be REAL -- the actual id-set
    # membership after the check must match the real eligible rows, not
    # just report that a check happened to run.
    for key in list(srv._FTS_REBUILD_CHECKED_PATHS):
        srv._FTS_REBUILD_CHECKED_PATHS[key] -= (srv._FTS_REBUILD_CHECK_TTL_SECONDS + 1)
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
