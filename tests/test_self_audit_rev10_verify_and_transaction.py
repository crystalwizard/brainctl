"""Self-audit regression tests, 2026-09-14, against commit(candidate) built
on bf26e9f -- mirrors two real behaviors Ari's REV10 adversarial test file
(test_adversarial_fts_rev10.py, not part of this repo, evidence only) checks
under a different key name / function arity than this fix actually uses.

Neither of the two REV10 test failures against that external file is a real
correctness gap -- both behaviors are present and correct here, just checked
under this repo's own naming/API rather than Ari's pre-fix guess at them
(he wrote that file before seeing this commit's actual shape):

- His test ORs on `result.get('verification_failed')`; this repo's actual
  surfaced key is `index_verification_failed`, matching the existing
  `index_repair_failed` naming convention rather than introducing an
  inconsistent unprefixed sibling.
- His test directly unpacks `_verify_and_repair_stale_matches`'s return as
  a 2-tuple (`result, failed = ...`); this fix's real signature returns a
  3-tuple, `(results, repair_failed, verification_failed)`, so the two
  failure modes (a repair that was attempted and failed, vs. correctness
  that couldn't be established at all) are distinguishable rather than
  conflated into one flag.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "src" / "agentmemory" / "db" / "init_schema.sql").read_text(encoding="utf-8")


def _seed_stale_index_db(db_path):
    now = "2026-01-01T00:00:00"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
        "VALUES ('t','t','test','active',?,?)", (now, now),
    )
    conn.execute(
        "INSERT INTO memories (id, content, category, tags, indexed, agent_id, created_at, updated_at) "
        "VALUES (1,'realneedle','lesson','',1,'t',?,?)", (now, now),
    )
    conn.commit()
    conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('delete-all')")
    conn.execute(
        "INSERT INTO memories_fts(rowid,content,category,tags) VALUES(1,'obsoleteword','lesson','')"
    )
    conn.commit()
    conn.close()


def test_verifier_connection_failure_surfaces_index_verification_failed(tmp_path, monkeypatch):
    """R10-B3: a stale index PLUS a verifier that itself can't run (its
    :memory: connection denied) must not collapse into an ordinary-looking
    `ok: True, count: 0` -- the same silent-wrong-answer shape this whole
    branch exists to prevent, just at the verifier layer instead of the
    index layer."""
    import agentmemory.mcp_server as srv

    db_path = tmp_path / "verification-error.db"
    _seed_stale_index_db(db_path)

    monkeypatch.setattr(srv, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(srv, "_DB_PATH_LOCKED", True, raising=False)
    # Isolate the verify-and-repair path under test from _cold_start_check_once's
    # OWN rebuild attempt (a real file-backed connection, unaffected by the
    # :memory:-only fault injected below) -- without this, cold-start's
    # unrelated rebuild would silently fix the seeded staleness before the
    # verify path ever ran, masking exactly the scenario this test exists
    # to exercise. Simulates "already checked recently" (a warm, long-lived
    # process), same real-world case a same-stamp restore actually defeats.
    monkeypatch.setattr(srv, "_cold_start_check_once", lambda conn, path: False)

    real_connect = sqlite3.connect

    def failing_memory_connect(database, *args, **kwargs):
        if database == ":memory:":
            raise sqlite3.OperationalError("independent injected verifier allocation failure")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", failing_memory_connect)

    result = srv.tool_memory_search(agent_id="t", query="realneedle", benchmark=True)

    assert result["ok"] is True, "search itself must not raise/crash on a denied verifier connection"
    assert result["count"] == 0, "sanity: the stale index genuinely can't be verified this call"
    assert result.get("index_verification_failed") is True, (
        "a verifier-connection failure on top of a stale index must be surfaced, "
        "not silently reported as an ordinary successful zero-result search"
    )


def test_verify_and_repair_preserves_callers_transaction(tmp_path):
    """R10 additional finding: the verify-and-repair helper's own rebuild
    call must not commit work the CALLER already had pending in an open
    transaction -- same class of bug _commit_if_owned exists to prevent
    (R2-F1), reproduced one call site over by an unconditional db.commit()
    that ignored transaction ownership."""
    import agentmemory.mcp_server as srv

    db_path = tmp_path / "transaction.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    now = "2026-01-01T00:00:00"
    conn.execute(
        "INSERT INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
        "VALUES ('t','t','test','active',?,?)", (now, now),
    )
    conn.execute(
        "INSERT INTO memories (id, content, category, tags, indexed, agent_id, created_at, updated_at) "
        "VALUES (1,'actualneedle','lesson','',1,'t',?,?)", (now, now),
    )
    conn.commit()

    conn.execute("INSERT INTO workspace_config(key,value) VALUES('ari_pending','uncommitted')")
    assert conn.in_transaction

    results, repair_failed, verification_failed = srv._verify_and_repair_stale_matches(
        conn, "obsoleteword", [{"id": 1, "content": "actualneedle"}], lambda: [],
    )

    conn.rollback()
    exists = conn.execute(
        "SELECT value FROM workspace_config WHERE key='ari_pending'"
    ).fetchone()
    conn.close()

    assert exists is None, "verify-and-repair helper committed unrelated caller-owned work"
