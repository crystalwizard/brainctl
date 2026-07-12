"""Regression coverage for the dream_cycle timestamp bug (issue #168 / THE-65).

Root cause: `created_at` is written timezone-aware (via `_utc_now_iso()`, a
Z-suffixed UTC string), but `last_recalled_at` is written timezone-naive
(local `datetime.now()`, via `apply_recall_boost`). `hippocampus.parse_ts()`
parses the aware string into an aware datetime; `run_hebbian_pass`'s own
`now` and its `days_since()` calls mix aware and naive values and raise:

    TypeError: can't subtract offset-naive and offset-aware datetimes

This file reproduces the crash directly (no CLI, no copied production DB)
so it runs fast and deterministically in CI, and pins down the fix's
expected end state once the timestamp policy lands.
"""
from datetime import datetime

import pytest

from agentmemory.hippocampus import run_hebbian_pass


def _simulate_recall(brain, memory_id: int, when: datetime) -> None:
    """Write last_recalled_at the way apply_recall_boost actually does:
    naive local time, no tzinfo -- this is the other half of the bug."""
    db = brain._get_conn()
    db.execute(
        "UPDATE memories SET last_recalled_at = ?, recalled_count = recalled_count + 1 WHERE id = ?",
        (when.strftime("%Y-%m-%dT%H:%M:%S"), memory_id),
    )
    db.commit()


def test_hebbian_pass_survives_aware_created_at_naive_recalled_at(brain):
    """Two memories, recalled together (same 60s co-retrieval window),
    with the real production timestamp shapes: aware created_at (from
    brain.remember -> _utc_now_iso), naive last_recalled_at (from a
    simulated recall). This is exactly issue #168's crash condition.

    Pre-fix: raises TypeError. Post-fix: completes and reports the pair
    as a co-retrieval session.
    """
    mem_a = brain.remember("First co-recalled memory", category="lesson")
    mem_b = brain.remember("Second co-recalled memory", category="lesson")

    now = datetime.now()
    _simulate_recall(brain, mem_a, now)
    _simulate_recall(brain, mem_b, now)

    db = brain._get_conn()

    # This is the actual regression assertion. Before the timestamp-policy
    # fix lands, this call raises TypeError inside run_hebbian_pass's
    # days_since(now, created_at) at the co-retrieval-pair strengthening
    # step. That failure IS the reproduction -- once it passes, issue #168
    # is fixed for this call site.
    stats = run_hebbian_pass(db, now=now)

    assert stats["sessions_scanned"] >= 1
    assert stats["pairs_found"] >= 1


def test_hebbian_pass_naive_recalled_at_far_in_past_still_survives(brain):
    """Same shape, but the recall is 45 days old (outside the 30-day
    cutoff) -- confirms the fix doesn't just get lucky on same-day
    timestamps where the aware/naive delta happens to be small."""
    from datetime import timedelta

    mem_a = brain.remember("Old recalled memory", category="lesson")
    old_recall = datetime.now() - timedelta(days=45)
    _simulate_recall(brain, mem_a, old_recall)

    db = brain._get_conn()
    stats = run_hebbian_pass(db, now=datetime.now())

    # Outside the 30-day cutoff, so no session -- the point of this test
    # is that reaching this line at all means no TypeError was raised.
    assert stats["sessions_scanned"] == 0
