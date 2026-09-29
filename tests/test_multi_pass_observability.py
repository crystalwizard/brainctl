"""Public observability for discarded multi-pass enrichment."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import agentmemory.mcp_server as ms


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    db_file = tmp_path / "brain.db"
    from agentmemory.brain import Brain

    brain = Brain(db_path=str(db_file), agent_id="test")
    brain.remember(
        "caching strategy for the API endpoint reduces latency and improves throughput",
        category="convention",
    )
    brain.remember(
        "caching API diagnostics preserve verified primary search results",
        category="convention",
    )
    monkeypatch.setattr(ms, "DB_PATH", db_file)
    return db_file


def _search():
    return ms.tool_memory_search(
        agent_id="test",
        query="caching API",
        multi_pass=True,
    )


@pytest.mark.parametrize(
    "flag",
    ["index_verification_failed", "repair_verification_failed"],
)
def test_degraded_pass2_outcome_surfaces_skip(seeded_db, monkeypatch, flag):
    calls = 0
    verified_primary_ids = []

    def fake_verify(db, fts_q, fetch_full, row_filter, limit, run_primary):
        nonlocal calls
        calls += 1
        primary = run_primary()
        if calls == 1:
            verified_primary_ids.extend(m["id"] for m in primary)
            return {"ok": True, "results": primary, "flag": None}
        return {"ok": True, "results": primary, "flag": flag}

    monkeypatch.setattr(ms, "_verify_restore_time_order", fake_verify)

    result = _search()

    assert calls == 2
    assert result["ok"] is True
    assert result["multi_pass_skipped"] is True
    assert result["multi_pass_skip_reason"] == flag
    assert [m["id"] for m in result["memories"]] == verified_primary_ids


def test_rejected_pass2_without_flag_surfaces_stable_reason(seeded_db, monkeypatch):
    calls = 0
    verified_primary_ids = []

    def fake_verify(db, fts_q, fetch_full, row_filter, limit, run_primary):
        nonlocal calls
        calls += 1
        primary = run_primary()
        if calls == 1:
            verified_primary_ids.extend(m["id"] for m in primary)
            return {"ok": True, "results": primary, "flag": None}
        return {"ok": False, "results": primary, "flag": None}

    monkeypatch.setattr(ms, "_verify_restore_time_order", fake_verify)

    result = _search()

    assert result["multi_pass_skipped"] is True
    assert result["multi_pass_skip_reason"] == "verification_rejected"
    assert [m["id"] for m in result["memories"]] == verified_primary_ids


def test_raised_pass2_verification_surfaces_error_without_detail(seeded_db, monkeypatch):
    calls = 0
    verified_primary_ids = []

    def fake_verify(db, fts_q, fetch_full, row_filter, limit, run_primary):
        nonlocal calls
        calls += 1
        if calls == 1:
            primary = run_primary()
            verified_primary_ids.extend(m["id"] for m in primary)
            return {"ok": True, "results": primary, "flag": None}
        raise RuntimeError("private internal detail")

    monkeypatch.setattr(ms, "_verify_restore_time_order", fake_verify)

    result = _search()

    assert result["multi_pass_skipped"] is True
    assert result["multi_pass_skip_reason"] == "verification_error"
    assert "private internal detail" not in repr(result)
    assert [m["id"] for m in result["memories"]] == verified_primary_ids


def test_clean_pass2_omits_skip_fields(seeded_db, monkeypatch):
    def fake_verify(db, fts_q, fetch_full, row_filter, limit, run_primary):
        return {"ok": True, "results": run_primary(), "flag": None}

    monkeypatch.setattr(ms, "_verify_restore_time_order", fake_verify)

    result = _search()

    assert "multi_pass_skipped" not in result
    assert "multi_pass_skip_reason" not in result


def test_multi_pass_false_omits_skip_fields(seeded_db):
    result = ms.tool_memory_search(
        agent_id="test",
        query="caching API",
        multi_pass=False,
    )

    assert "multi_pass_skipped" not in result
    assert "multi_pass_skip_reason" not in result
