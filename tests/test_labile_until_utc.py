"""Regression coverage for the labile_until UTC/local mismatch in apply_decay
(THE-65 safety tranche, cluster 2, found during the 2026-07-12/13 audit -- this
is a live behavioral bug independent of the #168 crash, in a different column,
in a different function, silent rather than crashing).

ROOT CAUSE: labile_until (the reconsolidation/decay-immunity window) is
written elsewhere in the codebase (_impl.py, mcp_tools_meb.py) using SQLite's
own `'now'`, which is UTC. apply_decay's labile-window check compared that
UTC-anchored value against `now_sql`, derived from Python's local, naive
`datetime.now()`. On a machine whose local time lags UTC (this real
production machine included -- Arizona, UTC-7, no DST), the local `now_sql`
string reads numerically *earlier* than true UTC at the same instant. Since
the comparison is `labile_until > now_sql`, a labile_until that has already
passed in real UTC terms could still read as "in the future" relative to the
lagging local `now_sql` -- the memory stays protected from decay for roughly
the size of the local UTC offset (about 7 hours on this machine) longer than
the window was actually meant to last.

This is silent, not crashing -- confidence just doesn't decay when it should,
with no error or log line pointing at why. Unlike the #168 crash (which only
affects memories that have been recalled), any memory ever tagged with a
labile_until value is affected on every apply_decay pass.

FIX: apply_decay now derives a UTC-anchored comparison value from the same
`now` argument via `.astimezone(timezone.utc)`, independent of the local
`now_sql` used for the (already-established-as-UTC-vs-local-safe) day-scale
created_at/last_recalled_at math elsewhere in the function.
"""
from datetime import datetime, timedelta, timezone

import pytest

from agentmemory.hippocampus import apply_decay


def _remember_with_naive_created_at(brain, content: str, now: datetime) -> int:
    """Insert a memory with a naive (not `_utc_now_iso()`-aware) created_at.

    Deliberately not using `brain.remember()` here: it writes created_at via
    the real aware UTC writer, which would immediately hit the *separate*,
    still-unfixed #168 naive/aware TypeError inside apply_decay's own
    days_since(now, created_at) call (confirmed while writing this test --
    apply_decay is itself one of the "AT RISK, conditional" reader sites
    from the original writer inventory, not just run_hebbian_pass). That's
    a real, different bug, already tracked as THE-65 cluster 3 -- this test
    is specifically about the labile_until UTC/local mismatch and shouldn't
    be blocked on a different fix landing first. Writing created_at as naive
    (matching what a pre-fix production database actually contains for
    plenty of rows) keeps this test isolated to the one behavior it exists
    to check.
    """
    db = brain._get_conn()
    now_sql = now.strftime("%Y-%m-%dT%H:%M:%S")
    cur = db.execute(
        "INSERT INTO memories (agent_id, category, scope, content, confidence, "
        "temporal_class, memory_type, created_at, updated_at) "
        "VALUES (?, 'lesson', 'global', ?, 0.9, 'medium', 'episodic', ?, ?)",
        (brain.agent_id, content, now_sql, now_sql),
    )
    db.commit()
    return cur.lastrowid


def _set_labile_until(brain, memory_id: int, when_utc: datetime) -> None:
    """Write labile_until the way production code actually does it: a UTC
    timestamp string with no timezone suffix (matching _impl.py/
    mcp_tools_meb.py's `strftime(..., 'now', ...)` pattern -- SQLite's 'now'
    is UTC, formatted without an offset marker).
    """
    db = brain._get_conn()
    db.execute(
        "UPDATE memories SET labile_until = ? WHERE id = ?",
        (when_utc.strftime("%Y-%m-%dT%H:%M:%S"), memory_id),
    )
    db.commit()


def test_labile_window_that_has_closed_in_utc_no_longer_blocks_decay(brain):
    """The actual regression test.

    Sets labile_until to a value that has genuinely, unambiguously closed in
    real UTC terms (one minute before the true current UTC instant) and
    confirms apply_decay does NOT treat the memory as still-labile.

    Pre-fix on a UTC-behind machine: this memory would incorrectly be
    reported as skipped_labile (still "in the future" relative to the
    lagging local now_sql), and its confidence would not decay at all this
    pass -- that's the bug this test catches.

    Post-fix: the window is correctly recognized as closed; the memory goes
    through normal decay processing (skipped_labile stays 0, updated
    includes this memory).
    """
    now = datetime.now()
    mem_id = _remember_with_naive_created_at(
        brain, "Memory with a labile window that has already closed", now
    )
    now_utc = now.astimezone(timezone.utc)
    closed_one_minute_ago_utc = now_utc - timedelta(minutes=1)
    _set_labile_until(brain, mem_id, closed_one_minute_ago_utc)

    db = brain._get_conn()
    stats = apply_decay(db, now=now)

    # THE ACTUAL ASSERTION UNDER TEST: a labile window that has closed in
    # true UTC terms must not be reported as still-labile, regardless of
    # what the local wall-clock time reads on this machine.
    assert stats["skipped_labile"] == 0, (
        "memory with an already-closed labile_until was incorrectly treated "
        "as still labile -- UTC/local comparison mismatch"
    )
    assert stats["updated"] == 1


def test_labile_window_still_genuinely_open_still_blocks_decay(brain):
    """Companion test: confirms the fix doesn't just make the labile check a
    no-op. A window that is genuinely still open in real UTC terms (one
    hour in the future) must still protect the memory from decay.
    """
    now = datetime.now()
    mem_id = _remember_with_naive_created_at(
        brain, "Memory with a labile window that is genuinely still open", now
    )
    now_utc = now.astimezone(timezone.utc)
    still_open_one_hour_from_now_utc = now_utc + timedelta(hours=1)
    _set_labile_until(brain, mem_id, still_open_one_hour_from_now_utc)

    db = brain._get_conn()
    stats = apply_decay(db, now=now)

    assert stats["skipped_labile"] == 1
    assert stats["updated"] == 0
