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


def test_brain_handoff_is_api_and_caller_cannot_choose_origin(brain, tmp_path):
    """Grok's finding: the public Python API used to accept origin='wrap_up'."""
    hid = brain.handoff("g", "s", "l", "n")
    assert _row(tmp_path / "brain.db", "handoff_packets", hid)["origin"] == "api"
    with pytest.raises(TypeError):
        brain.handoff("g", "s", "l", "n", origin="wrap_up")
    with pytest.raises(TypeError):
        brain.handoff("g", "s", "l", "n", source_event_id=1)
    with pytest.raises(ValueError):
        brain._write_handoff("g", "s", "l", "n", origin="trusted")


def test_brain_trigger_rejects_caller_origin(brain):
    with pytest.raises(TypeError):
        brain.trigger("c", "kw", "a", origin="wrap_up")


def test_supplying_a_real_session_end_event_id_does_not_confer_wrap_up_origin(mcp_db):
    """source_event_id stays a caller-suppliable link on direct writes (it is a
    real feature); the trust-relevant field is origin, which is set server side."""
    db_file, b = mcp_db
    w = b.wrap_up("legit end", next_step="go")
    out = mcp_server.tool_handoff_add(
        agent_id="test-agent", goal="forged", current_state="s", open_loops="l",
        next_step="n", source_event_id=w["event_id"], origin="wrap_up",
    )
    assert out["ok"] is True
    row = _row(db_file, "handoff_packets", out["handoff_id"])
    assert row["origin"] == "direct"
    assert row["source_event_id"] == w["event_id"]


def test_resume_and_check_triggers_carry_notice(brain):
    brain.handoff("g", "s", "l", "n")
    brain.trigger("c", "banana", "a")
    matches = brain.check_triggers("a banana")
    assert matches and matches[0]["origin"] == "api"
    assert matches[0]["provenance_notice"]
    packet = brain.resume()
    assert packet["origin"] == "api"
    assert packet["provenance_notice"] == PROVENANCE_NOTICE
    assert brain.resume() == {}


def test_same_second_ties_resolve_to_higher_id_everywhere(mcp_db):
    db_file, b = mcp_db
    ids = []
    for goal in ("first", "second"):
        out = mcp_server.tool_handoff_add(
            agent_id="test-agent", goal=goal, current_state="s", open_loops="l", next_step="n",
        )
        ids.append(out["handoff_id"])
    conn = sqlite3.connect(str(db_file))
    conn.execute("UPDATE handoff_packets SET created_at = '2026-01-01T00:00:00'")
    conn.commit()
    conn.close()
    assert mcp_server.tool_handoff_latest(agent_id="test-agent")["id"] == ids[1]
    assert b.orient()["handoff"]["id"] == ids[1]
    assert b.resume()["id"] == ids[1]


def _cli(tmp_path, *args):
    import json
    import os
    import subprocess
    env = dict(os.environ, BRAIN_DB=str(tmp_path / "cli.db"), PYTHONPATH=str(SRC))
    r = subprocess.run([sys.executable, "-m", "agentmemory.cli", "-a", "cliagent", *args],
                       capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout[r.stdout.index("{"):])


def test_cli_reads_carry_origin_and_notice(tmp_path):
    import os
    import subprocess
    env = dict(os.environ, BRAIN_DB=str(tmp_path / "cli.db"), PYTHONPATH=str(SRC))
    subprocess.run([sys.executable, "-m", "agentmemory.cli", "init"], capture_output=True,
                   env=env, cwd=str(tmp_path), timeout=120)
    _cli(tmp_path, "handoff", "add", "--goal", "g", "--current-state", "s",
         "--open-loops", "l", "--next-step", "n")
    _cli(tmp_path, "trigger", "create", "when banana", "--keywords", "banana", "--action", "a")
    latest = _cli(tmp_path, "handoff", "latest")
    assert latest["origin"] == "cli" and latest["provenance_notice"] == PROVENANCE_NOTICE
    chk = _cli(tmp_path, "trigger", "check", "a banana")
    assert chk["matched_triggers"][0]["origin"] == "cli"
    assert chk["provenance_notice"] == PROVENANCE_NOTICE


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


def test_cli_search_triggered_memories_carry_notice(tmp_path):
    import os
    import subprocess
    env = dict(os.environ, BRAIN_DB=str(tmp_path / "cli.db"), PYTHONPATH=str(SRC))
    subprocess.run([sys.executable, "-m", "agentmemory.cli", "init"], capture_output=True,
                   env=env, cwd=str(tmp_path), timeout=120)
    _cli(tmp_path, "trigger", "create", "when banana", "--keywords", "banana", "--action", "do a thing")
    out = _cli(tmp_path, "search", "banana")
    assert out["triggered_memories"][0]["origin"] == "cli"
    assert out["provenance_notice"] == PROVENANCE_NOTICE
