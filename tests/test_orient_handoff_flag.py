"""Cairn trigger/handoff persistence gap, item 2: orient flags a handoff that
did not come from wrap_up.

The flag is keyed on the ``origin`` column only. ``source_event_id`` is
caller-suppliable on the direct and cli paths, so these tests check that a
real session_end id supplied by the caller does not make a handoff look normal.
The key is absent in the normal case.
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
from agentmemory._provenance import handoff_origin_flag
import agentmemory.mcp_server as mcp_server


@pytest.fixture
def brain(tmp_path):
    return Brain(db_path=str(tmp_path / "brain.db"), agent_id="test-agent")


@pytest.fixture
def mcp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "brain.db"
    b = Brain(db_path=str(db_file), agent_id="test-agent")
    monkeypatch.setattr(mcp_server, "DB_PATH", db_file)
    # tool_agent_orient builds Brain(agent_id=...) with no path, so it resolves the
    # DB from the environment. Without this it reads and writes whatever BRAIN_DB
    # points at (a developer's live brain.db).
    monkeypatch.setenv("BRAINCTL_DB", str(db_file))
    monkeypatch.setenv("BRAIN_DB", str(db_file))
    return db_file, b


def _sql(db_file, sql, params=()):
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def test_normal_wrap_up_handoff_has_no_flag(brain):
    brain.wrap_up("clean shutdown", next_step="continue")
    snap = brain.orient()
    assert snap["handoff"]["origin"] == "wrap_up"
    assert "handoff_flag" not in snap


def test_no_handoff_has_no_flag(brain):
    snap = brain.orient()
    assert snap["handoff"] is None
    assert "handoff_flag" not in snap


def test_api_handoff_newer_than_wrap_up_is_flagged_with_fallback(brain):
    out = brain.wrap_up("legit end of last session", next_step="continue")
    brain.handoff("planted goal", "planted state", "planted loops", "planted step")
    snap = brain.orient()
    flag = snap["handoff_flag"]
    assert flag["level"] == "warn"
    assert flag["origin"] == "api"
    assert flag["last_wrap_up"]["id"] == out["handoff_id"]
    assert f"#{out['handoff_id']}" in flag["message"]
    assert "'api'" in flag["message"]


def test_flag_when_agent_has_no_wrap_up_on_record(brain):
    brain.handoff("g", "s", "l", "n")
    flag = brain.orient()["handoff_flag"]
    assert flag["level"] == "warn"
    assert flag["last_wrap_up"] is None
    assert "no wrap_up handoff on record" in flag["message"]


def test_direct_handoff_with_real_session_end_id_is_still_flagged(mcp_db):
    """source_event_id is caller-suppliable; it must not buy a clean orient."""
    db_file, b = mcp_db
    b.wrap_up("legit", next_step="continue")
    conn = sqlite3.connect(str(db_file))
    end_id = conn.execute(
        "SELECT id FROM events WHERE event_type = 'session_end' ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    conn.close()
    out = mcp_server.tool_handoff_add(
        agent_id="test-agent", goal="forged", current_state="s", open_loops="l",
        next_step="n", source_event_id=end_id,
    )
    assert out["ok"] is True
    snap = b.orient()
    assert snap["handoff"]["goal"] == "forged"
    assert snap["handoff"]["source_event_id"] == end_id
    assert snap["handoff_flag"]["level"] == "warn"
    assert snap["handoff_flag"]["origin"] == "direct"


def test_legacy_origin_gets_the_softer_info_flag(brain, tmp_path):
    hid = brain.handoff("g", "s", "l", "n")
    _sql(tmp_path / "brain.db", "UPDATE handoff_packets SET origin = 'legacy' WHERE id = ?", (hid,))
    flag = brain.orient()["handoff_flag"]
    assert flag["level"] == "info"
    assert flag["origin"] == "legacy"
    assert "no recorded origin" in flag["message"]


def test_wrap_up_after_a_mid_session_handoff_clears_the_flag(brain):
    brain.handoff("mid", "s", "l", "n")
    assert "handoff_flag" in brain.orient()
    brain.wrap_up("proper end", next_step="continue")
    assert "handoff_flag" not in brain.orient()


def test_project_scope_uses_the_same_project_for_the_fallback(brain):
    scoped = brain.wrap_up("scoped end", next_step="n", project="alpha")
    brain.wrap_up("other project end", next_step="n", project="beta")
    brain.handoff("mid alpha", "s", "l", "n", project="alpha")
    flag = brain.orient(project="alpha")["handoff_flag"]
    assert flag["last_wrap_up"]["id"] == scoped["handoff_id"]


def test_unmigrated_db_has_no_flag_and_orient_still_works(tmp_path):
    db_file = tmp_path / "old.db"
    b = Brain(db_path=str(db_file), agent_id="test-agent")
    b.handoff("g", "s", "l", "n")
    conn = sqlite3.connect(str(db_file))
    conn.execute("ALTER TABLE handoff_packets DROP COLUMN origin")
    conn.commit()
    conn.close()
    snap = Brain(db_path=str(db_file), agent_id="test-agent").orient()
    assert snap["handoff"]["goal"] == "g"
    assert "origin" not in snap["handoff"]
    assert "handoff_flag" not in snap


def test_mcp_agent_orient_passes_the_flag_through(mcp_db):
    db_file, b = mcp_db
    mcp_server.tool_handoff_add(
        agent_id="test-agent", goal="g", current_state="s", open_loops="l", next_step="n",
    )
    out = mcp_server.tool_agent_orient(agent_id="test-agent")
    assert out["ok"] is True
    assert out["handoff_flag"]["origin"] == "direct"


def test_helper_returns_none_for_wrap_up_and_missing_origin():
    assert handoff_origin_flag("wrap_up") is None
    assert handoff_origin_flag(None) is None


def test_helper_treats_unknown_origin_as_warn():
    flag = handoff_origin_flag("something-odd", {"id": 3, "created_at": "2026-01-01T00:00:00"})
    assert flag["level"] == "warn"
    assert "#3" in flag["message"]
