"""Cairn trigger/handoff persistence gap, item 1: write-path provenance.

Handoffs and triggers are agent-authored text that resurfaces at the next
session start. Migration 087 adds an ``origin`` column to both tables; every
write path stamps it, and every read that surfaces the text carries a fixed
notice. These tests lock in: the stamp per write path, the read-side fields,
and that an unmigrated DB (no ``origin`` column) keeps working unchanged.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentmemory.brain import Brain
from agentmemory._provenance import PROVENANCE_NOTICE
import agentmemory.mcp_server as mcp_server

MIGRATION_087 = ROOT / "db" / "migrations" / "087_handoff_trigger_origin.sql"


@pytest.fixture
def brain(tmp_path):
    return Brain(db_path=str(tmp_path / "brain.db"), agent_id="test-agent")


@pytest.fixture
def mcp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "brain.db"
    b = Brain(db_path=str(db_file), agent_id="test-agent")
    monkeypatch.setattr(mcp_server, "DB_PATH", db_file)
    return db_file, b


def _cols(db_file, table):
    conn = sqlite3.connect(str(db_file))
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def _row(db_file, table, row_id):
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone())
    finally:
        conn.close()


def test_fresh_db_has_origin_columns(tmp_path):
    db_file = tmp_path / "brain.db"
    Brain(db_path=str(db_file), agent_id="a")
    assert "origin" in _cols(db_file, "handoff_packets")
    assert "origin" in _cols(db_file, "memory_triggers")


def test_wrap_up_handoff_is_stamped_and_linked_to_its_session_end_event(brain, tmp_path):
    out = brain.wrap_up("did a thing", next_step="do the next thing")
    row = _row(tmp_path / "brain.db", "handoff_packets", out["handoff_id"])
    assert row["origin"] == "wrap_up"
    assert row["source_event_id"] == out["event_id"]


def test_brain_handoff_defaults_to_api_and_rejects_unknown_origin(brain, tmp_path):
    hid = brain.handoff("g", "s", "l", "n")
    assert _row(tmp_path / "brain.db", "handoff_packets", hid)["origin"] == "api"
    with pytest.raises(ValueError):
        brain.handoff("g", "s", "l", "n", origin="trusted")


def test_brain_trigger_is_stamped_api(brain, tmp_path):
    tid = brain.trigger("when x", "kw", "do y")
    assert _row(tmp_path / "brain.db", "memory_triggers", tid)["origin"] == "api"


def test_orient_surfaces_origin_and_notice(brain):
    brain.wrap_up("session summary", next_step="continue")
    brain.trigger("when x", "kw", "do y")
    snap = brain.orient()
    assert snap["provenance_notice"] == PROVENANCE_NOTICE
    assert snap["handoff"]["origin"] == "wrap_up"
    assert snap["handoff"]["source_event_id"] is not None
    assert snap["triggers"][0]["origin"] == "api"


def test_orient_shows_a_mid_session_handoff_as_not_wrap_up(brain):
    """The planted-handoff shape: written mid-session through the direct path."""
    brain.wrap_up("legit end of last session", next_step="continue")
    brain.handoff("planted goal", "planted state", "planted loops", "planted step")
    snap = brain.orient()
    assert snap["handoff"]["goal"] == "planted goal"
    assert snap["handoff"]["origin"] == "api"
    assert snap["handoff"]["source_event_id"] is None


def test_mcp_handoff_add_is_direct_and_latest_carries_origin_and_notice(mcp_db):
    db_file, _ = mcp_db
    out = mcp_server.tool_handoff_add(
        agent_id="test-agent", goal="g", current_state="s", open_loops="l", next_step="n",
    )
    assert out["ok"] is True
    assert _row(db_file, "handoff_packets", out["handoff_id"])["origin"] == "direct"
    latest = mcp_server.tool_handoff_latest(agent_id="test-agent")
    assert latest["origin"] == "direct"
    assert latest["provenance_notice"] == PROVENANCE_NOTICE


def test_mcp_handoff_latest_empty_has_no_notice_and_stays_empty(mcp_db):
    assert mcp_server.tool_handoff_latest(agent_id="test-agent") == {}


def test_mcp_trigger_create_is_direct_and_check_carries_origin_and_notice(mcp_db):
    db_file, _ = mcp_db
    out = mcp_server.tool_trigger_create(
        agent_id="test-agent", condition="c", keywords="banana", action="a",
    )
    assert out["ok"] is True
    assert _row(db_file, "memory_triggers", out["trigger_id"])["origin"] == "direct"
    chk = mcp_server.tool_trigger_check(agent_id="test-agent", query="a banana appears")
    assert chk["provenance_notice"] == PROVENANCE_NOTICE
    assert chk["matched_triggers"][0]["origin"] == "direct"


def test_unmigrated_db_without_origin_columns_keeps_working(tmp_path, monkeypatch):
    """An agent DB that hasn't run migration 087 yet must not lose its handoff
    at orient, and every write path must still succeed (no origin stamped)."""
    db_file = tmp_path / "brain.db"
    b = Brain(db_path=str(db_file), agent_id="test-agent")
    conn = sqlite3.connect(str(db_file))
    conn.execute("ALTER TABLE handoff_packets DROP COLUMN origin")
    conn.execute("ALTER TABLE memory_triggers DROP COLUMN origin")
    conn.commit()
    conn.close()
    monkeypatch.setattr(mcp_server, "DB_PATH", db_file)

    b2 = Brain(db_path=str(db_file), agent_id="test-agent")
    b2.wrap_up("old-style end", next_step="go")
    b2.trigger("c", "kw", "act")
    snap = b2.orient()
    assert snap["handoff"] is not None
    assert "origin" not in snap["handoff"]
    assert snap["triggers"] and "origin" not in snap["triggers"][0]

    assert mcp_server.tool_handoff_add(
        agent_id="test-agent", goal="g", current_state="s", open_loops="l", next_step="n",
    )["ok"] is True
    assert mcp_server.tool_trigger_create(
        agent_id="test-agent", condition="c", keywords="k", action="a",
    )["ok"] is True


def test_migration_087_marks_preexisting_rows_legacy(tmp_path):
    db_file = tmp_path / "brain.db"
    b = Brain(db_path=str(db_file), agent_id="test-agent")
    conn = sqlite3.connect(str(db_file))
    conn.execute("ALTER TABLE handoff_packets DROP COLUMN origin")
    conn.execute("ALTER TABLE memory_triggers DROP COLUMN origin")
    conn.commit()
    conn.close()
    b2 = Brain(db_path=str(db_file), agent_id="test-agent")
    hid = b2.handoff("g", "s", "l", "n")
    tid = b2.trigger("c", "kw", "a")

    conn = sqlite3.connect(str(db_file))
    conn.executescript(MIGRATION_087.read_text(encoding="utf-8"))
    conn.close()
    assert _row(db_file, "handoff_packets", hid)["origin"] == "legacy"
    assert _row(db_file, "memory_triggers", tid)["origin"] == "legacy"
