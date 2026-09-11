"""Regression tests for Ari's independent adversarial REV3 findings
(2026-09-10), against commit e48054e + aea16da. R3-B4 (conn.in_transaction
duck-typed-proxy AttributeError) was already fixed by aea16da before REV3
ran and is covered by the existing test_helper_rebuilds_on_database_error;
no new test needed for it here. R3-B1's own test lives in
test_ari_rev1_findings.py alongside F7/R2-F3, since they're the same
underlying _cold_start_check_once mechanism.

FINDINGS_REV3.md: F:\\GPT Codex\\reviews\\brainctl-fts-fix-2026-09-10\\FINDINGS_REV3.md
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "src" / "agentmemory" / "db" / "init_schema.sql").read_text(encoding="utf-8")


def _seed_db(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    now = "2026-09-10T12:00:00"
    conn.execute(
        "INSERT INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
        "VALUES ('t','t','test','active',?,?)", (now, now),
    )
    conn.execute(
        "INSERT INTO memories (id, content, category, tags, indexed, confidence, agent_id, created_at, updated_at) "
        "VALUES (1,'findable public content','general','',1,1.0,'t',?,?)", (now, now),
    )
    conn.execute(
        "INSERT INTO memories (id, content, category, tags, indexed, confidence, agent_id, created_at, updated_at) "
        "VALUES (2,'constructonly secret content','general','',0,1.0,'t',?,?)", (now, now),
    )
    conn.commit()
    conn.close()


def _search_args(**overrides):
    base = dict(
        query="content", exact=True, limit=20, no_recency=True,
        scope=None, category=None, agent="test", output="json",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


# --- R3-B2: exact-search CLI path must enforce eligibility, via the real handler ---

def test_r3_b2_real_cli_exact_search_excludes_construct_only(locked_db_path, capsys):
    """Ari's finding: the previous R2-B1 'defense in depth' test manually
    built and ran corrected SQL rather than calling any production function,
    so it could never have caught this. This calls the real cmd_memory_search
    CLI handler (the actual code path a user hits with `brainctl search --exact`)
    against a locked, disposable database."""
    import agentmemory._impl as impl

    _seed_db(locked_db_path)

    impl.cmd_memory_search(_search_args(query="content"))
    out = json.loads(capsys.readouterr().out)

    contents = [r["content"] for r in out]
    assert "findable public content" in contents
    assert "constructonly secret content" not in contents, (
        "real cmd_memory_search(exact=True) must never surface indexed=0 content (R3-B2)"
    )


def test_r3_b2_real_cli_exact_search_still_finds_eligible_content(locked_db_path, capsys):
    """Companion to the above: confirm the eligibility fix didn't just make
    exact search return nothing at all."""
    import agentmemory._impl as impl

    _seed_db(locked_db_path)

    impl.cmd_memory_search(_search_args(query="findable"))
    out = json.loads(capsys.readouterr().out)

    assert len(out) == 1
    assert out[0]["content"] == "findable public content"


# --- R3-B3: a direct DB_PATH patch (no lock flag) must still be safe ---

def test_r3_b3_direct_db_path_patch_survives_ambient_brain_db(monkeypatch, tmp_path):
    """Ari's finding: the R2-B2 fixture (locked_db_path) only protects tests
    that opt into it. The far more common existing pattern -- a test directly
    doing `monkeypatch.setattr(impl, "DB_PATH", ...)` without also touching
    _DB_PATH_LOCKED -- got no protection at all. A real get_db() call with an
    ambient BRAIN_DB set still opened the ambient database instead of the
    intended one.

    This calls the real get_db() (not a reimplementation of its gate logic,
    which is exactly what Ari flagged the R2-B2 tests for doing), through the
    exact vulnerable pattern: a direct DB_PATH patch, no lock flag touched."""
    import agentmemory._impl as impl

    intended = tmp_path / "intended.db"
    ambient = tmp_path / "ambient.db"
    _seed_db(intended)
    _seed_db(ambient)

    monkeypatch.setenv("BRAIN_DB", str(ambient))
    # The exact pre-existing pattern across the test suite: patch DB_PATH
    # directly, nothing else. No _DB_PATH_LOCKED = True anywhere.
    monkeypatch.setattr(impl, "DB_PATH", intended, raising=False)

    conn = impl.get_db()
    try:
        row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        # Both seeded dbs have 2 rows, so count alone can't distinguish them --
        # assert on DB_PATH itself, which is what get_db() must not have
        # silently reassigned out from under the direct patch.
        assert impl.DB_PATH == intended, (
            "a direct DB_PATH patch must survive an ambient BRAIN_DB even "
            "without _DB_PATH_LOCKED being set explicitly (R3-B3)"
        )
    finally:
        conn.close()


def test_r3_b3_env_only_change_still_works_for_unpatched_db_path(monkeypatch, tmp_path):
    """The other half of R3-B3's fix must not have broken the legitimate,
    widely-used pattern (test_federation.py and others): a test that only
    sets an env var, never touching DB_PATH directly, still expects get_db()
    to pick up that env var. This is exactly what _DB_PATH_DEFAULT is for --
    DB_PATH is still at its as-imported default in this scenario."""
    import agentmemory._impl as impl

    if not hasattr(impl, "_DB_PATH_DEFAULT"):
        pytest.skip("_DB_PATH_DEFAULT doesn't exist on this commit -- new functionality")

    target = tmp_path / "via-env-only.db"
    _seed_db(target)
    monkeypatch.setattr(impl, "_DB_PATH_LOCKED", False, raising=False)
    monkeypatch.setattr(impl, "DB_PATH", impl._DB_PATH_DEFAULT, raising=False)
    monkeypatch.setenv("BRAIN_DB", str(target))

    conn = impl.get_db()
    try:
        assert impl.DB_PATH == target, (
            "an env-var-only change (DB_PATH left at its default) must still "
            "re-resolve through get_db() -- this is the existing, legitimate "
            "usage pattern, not the bug"
        )
    finally:
        conn.close()
