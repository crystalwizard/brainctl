"""Tests for FTS index seeding on cold-start ``init`` (issue #151).

External-content FTS5 (``memories_fts``, ``content=memories``) starts with an
empty inverted index. ``cmd_init`` applied migrations but never seeded the
index, and ``_ensure_fts_index_consistent`` only rebuilt when the FTS5
``'integrity-check'`` raised — an *empty* index is consistent, not corrupt,
so a populated-but-unindexed DB returned nothing from ``search`` with no
error.

Scope: the init-seed + under-population auto-heal + repair migration. (The
CLI ``add`` -> ``search`` end-to-end path additionally depends on the scoped
memories_fts update triggers from issue #152 / PR #166 — the unscoped
triggers erode a freshly inserted entry via post-insert metadata UPDATEs —
so that flow is not asserted here.)
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

mcp_server = pytest.importorskip("agentmemory.mcp_server")

MIGRATION_084 = ROOT / "db" / "migrations" / "084_fts_cold_start_rebuild.sql"

BRAINCTL = [
    sys.executable,
    "-c",
    "import sys; sys.path.insert(0, 'src'); from agentmemory.cli import main; "
    "sys.argv = ['brainctl'] + sys.argv[1:]; main()",
]


def _run_brainctl(*args, db_path=None):
    env = os.environ.copy()
    if db_path:
        env["BRAIN_DB"] = db_path
    result = subprocess.run(
        BRAINCTL + list(args), capture_output=True, text=True, cwd=str(ROOT), env=env
    )
    assert result.returncode == 0, f"{args}: {result.stderr}\n{result.stdout}"
    return result.stdout.strip()


def test_cmd_init_seeds_fts_index(tmp_path):
    """A fresh ``init`` must prime the external-content FTS5 index for real
    (eligible) content -- every indexed=1, non-retired row is indexed.

    Corrected 2026-09-10 per Ari's REV1 finding B3: the previous version of
    this test asserted docsize == total memories count, which only ever
    passed because a pre-fix rebuild also leaked cmd_init's own
    deliberately-indexed=0 sentinel row ("Do not surface in regular
    retrieval") into the raw FTS index. That was validating the bug, not
    the fix. This version seeds one real, eligible memory directly and
    checks only that eligible content is indexed -- the sentinel is
    correctly expected to stay out.
    """
    db = str(tmp_path / "brain.db")
    _run_brainctl("init", "--path", db)

    conn = sqlite3.connect(db)
    now = "2026-09-10T12:00:00"
    conn.execute(
        "INSERT INTO memories (id, content, category, tags, indexed, agent_id, created_at, updated_at) "
        "VALUES (100, 'real eligible content for cold-start seeding', 'general', '', 1, 'test', ?, ?)",
        (now, now),
    )
    conn.commit()
    _run_brainctl("init", "--path", db)  # idempotent re-run should (re-)seed without duplicating

    eligible = conn.execute(
        "SELECT count(*) FROM memories WHERE indexed = 1 AND retired_at IS NULL"
    ).fetchone()[0]
    docsize = conn.execute("SELECT count(*) FROM memories_fts_docsize").fetchone()[0]
    indexed_ids = set(r[0] for r in conn.execute("SELECT rowid FROM memories_fts_docsize").fetchall())
    sentinel_indexed = 1 in indexed_ids
    conn.close()

    assert eligible >= 1, "test seeded at least one eligible memory"
    assert 100 in indexed_ids, "the real eligible memory must be indexed after init"
    assert docsize == eligible, "docsize must equal exactly the eligible set, no more"
    assert not sentinel_indexed, "the indexed=0 sentinel memory must never surface in the raw FTS index"


def _seed_unindexed(db_path: Path) -> sqlite3.Connection:
    """Minimal memories + external-content memories_fts with an EMPTY index
    (mirrors init_schema.sql; the FTS inverted index is never rebuilt)."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY, agent_id TEXT, content TEXT,
            category TEXT, tags TEXT, indexed INTEGER DEFAULT 1, retired_at INTEGER
        );
        CREATE VIRTUAL TABLE memories_fts USING fts5(
            content, category, tags,
            content=memories, content_rowid=id,
            tokenize='porter unicode61'
        );
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO memories(id, agent_id, content, category, tags) VALUES (?,?,?,?,?)",
        [
            (1, "alice", "Kelly village infrastructure overview", "project", ""),
            (2, "alice", "Howler playtime morning routine", "preference", ""),
            (3, "alice", "API rate limits 100/15s", "integration", ""),
        ],
    )
    conn.commit()
    return conn


def test_ensure_consistent_rebuilds_underpopulated_index(tmp_path):
    """Helper must rebuild when the index is under-populated (docsize <
    active-indexed count), not only when integrity-check raises.

    On the pre-fix code an empty external-content index is 'consistent',
    so the helper returned False and the memories stayed unfindable.
    """
    db_path = tmp_path / "brain.db"
    conn = _seed_unindexed(db_path)

    pre = conn.execute(
        "SELECT count(*) FROM memories_fts WHERE memories_fts MATCH 'kelly'"
    ).fetchone()[0]
    assert pre == 0, "precondition: empty index must not MATCH"

    rebuilt = mcp_server._ensure_fts_index_consistent(conn)
    assert rebuilt is True

    post = conn.execute(
        "SELECT count(*) FROM memories_fts WHERE memories_fts MATCH 'kelly'"
    ).fetchone()[0]
    assert post == 1


def test_ensure_consistent_no_rebuild_when_fully_indexed(tmp_path):
    """When docsize already matches the active count and the index is
    healthy, the helper is a no-op (guards against needless rebuilds)."""
    db_path = tmp_path / "brain.db"
    conn = _seed_unindexed(db_path)
    conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
    conn.commit()

    assert mcp_server._ensure_fts_index_consistent(conn) is False


def test_migration_084_rebuilds_unindexed_db(tmp_path):
    """Applying migration 084 to a populated-but-unindexed DB rebuilds the
    FTS index and records the version (idempotent)."""
    db_path = tmp_path / "brain.db"
    conn = _seed_unindexed(db_path)
    sql = MIGRATION_084.read_text()

    conn.executescript(sql)
    conn.commit()

    hits = conn.execute(
        "SELECT count(*) FROM memories_fts WHERE memories_fts MATCH 'kelly'"
    ).fetchone()[0]
    assert hits == 1
    ver = conn.execute(
        "SELECT count(*) FROM schema_version WHERE version = 84"
    ).fetchone()[0]
    assert ver == 1

    # idempotent re-apply
    conn.executescript(sql)
    conn.commit()
    assert conn.execute(
        "SELECT count(*) FROM schema_version WHERE version = 84"
    ).fetchone()[0] == 1
