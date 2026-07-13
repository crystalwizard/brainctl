"""Regression coverage for a concurrent-invocation crash in run_hebbian_pass's
legacy-column upgrade guard (THE-65 safety tranche, cluster 5).

CONTEXT: init_schema.sql already declares knowledge_edges.last_reinforced_at,
co_activation_count, and weight_updated_at, so fresh installs never hit this
path. It exists purely as an upgrade guard for databases created before those
columns existed. There is no db/migrations/*.sql file for these columns
either (confirmed via grep) -- the runtime guard in run_hebbian_pass is the
ONLY place a legacy DB ever picks them up.

ROOT CAUSE: the guard is a check-then-act race with no lock and no error
handling:

    if not has_column(db, "knowledge_edges", "last_reinforced_at"):
        db.execute("ALTER TABLE knowledge_edges ADD COLUMN last_reinforced_at TEXT")

GPT's cross-correlation review (THE-65, 2026-07-12) independently found that
scheduler.py's cron path and the dream-daemon can both call run_hebbian_pass
against the same brain.db with no coordination. If two such invocations race
on a legacy DB, both can observe has_column() == False before either commits
its ALTER TABLE. The loser then hits a real sqlite3.OperationalError
("duplicate column name") from an ALTER TABLE that was never wrapped in a
try/except -- crashing the entire hebbian pass, not just the column-add step.

FIX: each of the three ALTER TABLE ADD COLUMN statements is now wrapped in a
try/except that swallows only the "duplicate column name" OperationalError
(the same tolerance already used by migrate.py's _apply_sql for the identical
error class), and re-raises anything else.
"""
import sqlite3

import pytest

from agentmemory import hippocampus
from agentmemory.brain import Brain


_HEBBIAN_COLUMNS = (
    ("last_reinforced_at", "TEXT"),
    ("co_activation_count", "INTEGER DEFAULT 0"),
    ("weight_updated_at", "TEXT"),
)


def _make_legacy_db(tmp_path):
    """Fresh brain.db, then drop the hebbian columns to simulate a
    pre-upgrade database (the only real-world condition where
    run_hebbian_pass's ALTER TABLE guard ever actually fires)."""
    db_file = tmp_path / "brain.db"
    Brain(db_path=str(db_file), agent_id="test-agent")

    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    for col, _decl in _HEBBIAN_COLUMNS:
        conn.execute(f"ALTER TABLE knowledge_edges DROP COLUMN {col}")
    conn.commit()
    return db_file, conn


def test_hebbian_columns_missing_on_legacy_db(tmp_path):
    """Sanity check the fixture actually reproduces a pre-upgrade DB."""
    _db_file, conn = _make_legacy_db(tmp_path)
    for col, _decl in _HEBBIAN_COLUMNS:
        assert not hippocampus.has_column(conn, "knowledge_edges", col)


def test_hebbian_pass_survives_concurrent_column_add_race(tmp_path, monkeypatch):
    """Two orchestrators racing run_hebbian_pass on a legacy DB must not
    crash when the loser's has_column() check ran before the winner's ALTER
    TABLE committed. Simulated deterministically: the winner really adds the
    columns first, then the loser's has_column check is forced to report
    "missing" (its real, stale value from before the winner committed),
    reproducing the exact ALTER TABLE call the loser's process would make.
    """
    _db_file, conn = _make_legacy_db(tmp_path)

    # Racer A (e.g. scheduler.py's cron) wins the race and adds the columns.
    for col, decl in _HEBBIAN_COLUMNS:
        conn.execute(f"ALTER TABLE knowledge_edges ADD COLUMN {col} {decl}")
    conn.commit()
    for col, _decl in _HEBBIAN_COLUMNS:
        assert hippocampus.has_column(conn, "knowledge_edges", col)

    # Racer B (e.g. the dream-daemon) is the one under test. Its has_column
    # check ran before racer A's commit -- force that stale "missing" view,
    # which is exactly what a real racing process would have seen.
    monkeypatch.setattr(hippocampus, "has_column", lambda *a, **kw: False)

    # Must not raise, and must complete the pass normally rather than crash
    # on the ALTER TABLE the stale check triggers.
    stats = hippocampus.run_hebbian_pass(conn)
    assert isinstance(stats, dict)
    assert "edges_strengthened" in stats


def test_hebbian_pass_still_adds_columns_on_real_legacy_db(tmp_path):
    """Non-race path: a genuinely legacy DB (no racer) still gets the
    columns added by run_hebbian_pass, same as before this fix."""
    _db_file, conn = _make_legacy_db(tmp_path)

    hippocampus.run_hebbian_pass(conn)

    for col, _decl in _HEBBIAN_COLUMNS:
        assert hippocampus.has_column(conn, "knowledge_edges", col)
