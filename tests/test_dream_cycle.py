"""Regression coverage for the dream_cycle timestamp bug (issue #168 / THE-65).

Written by Claude, 2026-07-12, as the first deliverable of the 3-person
(Kelly/Claude/GPT) effort to fix brainctl's dream_cycle ourselves rather than
wait on upstream (Terrance's repo had gone quiet -- see project_brainctl_issues.md
for the full history of that decision).

ROOT CAUSE (why this bug exists at all):
brainctl writes two different kinds of memory timestamp, and they don't agree
on timezone-awareness:
  - `created_at` is written by `_utc_now_iso()` (defined identically in
    brain.py, _impl.py, migrate.py) -- a Z-suffixed UTC string, e.g.
    "2026-07-12T21:16:51Z". `hippocampus.parse_ts()` parses this into a
    timezone-AWARE Python datetime.
  - `last_recalled_at` is written by `apply_recall_boost` using plain
    `datetime.now()` -- local time, no tzinfo at all. Timezone-NAIVE.

Python refuses to subtract an aware datetime from a naive one -- that's a
deliberate safety feature (comparing them without knowing the naive one's
timezone would silently produce a wrong answer), but it means any code path
that mixes the two blows up instead of just being wrong. `hippocampus.days_since()`
does exactly that mix, and `run_hebbian_pass` (part of the NREM phase of the
three-phase dream cycle) is the first place that combination actually gets
exercised on real data -- specifically for any memory that has ever been
recalled, since that's what writes the naive last_recalled_at in the first place.

The exact crash:
    TypeError: can't subtract offset-naive and offset-aware datetimes

WHY THIS TEST FILE EXISTS AND HOW IT WORKS:
The bug was first found by actually running dream_cycle by hand against a copy
of a real agent's brain.db (2026-07-04, see project_brainctl_issues.md). That's
a fine way to *discover* a bug but a slow, manual way to *guard against it
recurring* -- so this file reproduces the exact same crash condition
deterministically, in-process, using the project's own `brain` pytest fixture
instead of a copied production database. That means: no external file
dependency, runs in ~13 seconds, and will fail loudly in CI if anyone
reintroduces a naive/aware mismatch anywhere near this code path.

Before any fix lands, the first test below is EXPECTED TO FAIL -- that failure
is the reproduction. Once the timestamp-policy fix (tracked separately, see
project_brainctl_issues.md's 2026-07-12 update) actually lands, this test
should pass and stays in the suite permanently as a guard against regression.
"""
from datetime import datetime

import pytest

from agentmemory.hippocampus import run_hebbian_pass


def _simulate_recall(brain, memory_id: int, when: datetime) -> None:
    """Write last_recalled_at exactly the way production code does it.

    We can't just call brain's real "recall" path here without dragging in a
    lot of unrelated machinery, so this helper reproduces the one part that
    actually matters for this bug: `apply_recall_boost` (in hippocampus.py)
    writes last_recalled_at using naive `datetime.now().strftime(...)`, with
    no timezone info attached at all. That's the "naive" half of the
    aware/naive mismatch that causes issue #168 -- created_at (written
    elsewhere, via _utc_now_iso()) is the "aware" half. Reproducing this by
    hand, via direct SQL, keeps the test fast and focused on the timestamp
    bug specifically, rather than testing the whole recall-boost code path.
    """
    db = brain._get_conn()
    db.execute(
        "UPDATE memories SET last_recalled_at = ?, recalled_count = recalled_count + 1 WHERE id = ?",
        (when.strftime("%Y-%m-%dT%H:%M:%S"), memory_id),
    )
    db.commit()


def test_hebbian_pass_survives_aware_created_at_naive_recalled_at(brain):
    """The actual regression test for issue #168.

    Sets up the exact real-world condition that triggers the crash: two
    memories that were recalled together (within run_hebbian_pass's 60-second
    co-retrieval window), so the code tries to compare their aware
    `created_at` against the naive `now` it's working with.

    Pre-fix: this raises TypeError partway through run_hebbian_pass, at the
    days_since(now, created_at) call used to check the "critical period"
    edge-strengthening rule (memories under 7 days old get a bigger weight
    bump). That crash IS the reproduction -- we're not asserting the crash
    happens, we're asserting the function completes successfully, which it
    currently does not.

    Post-fix: should complete normally and report the two memories as a
    real co-retrieval session with a strengthened edge between them.
    """
    # brain.remember() uses the REAL production writer for created_at
    # (_utc_now_iso, timezone-aware) -- deliberately not hand-crafting this
    # timestamp ourselves, so the test stays honest about what production
    # code actually writes.
    mem_a = brain.remember("First co-recalled memory", category="lesson")
    mem_b = brain.remember("Second co-recalled memory", category="lesson")

    # Both memories "recalled" at the same moment -- puts them in the same
    # 60-second co-retrieval window that run_hebbian_pass looks for.
    now = datetime.now()
    _simulate_recall(brain, mem_a, now)
    _simulate_recall(brain, mem_b, now)

    db = brain._get_conn()

    # THE ACTUAL ASSERTION UNDER TEST: calling run_hebbian_pass with this
    # exact aware-created_at / naive-last_recalled_at combination is what
    # currently raises TypeError. Everything above this line is just setup;
    # this line is the reproduction itself.
    stats = run_hebbian_pass(db, now=now)

    # Once the fix lands, these confirm the function didn't just avoid
    # crashing -- it actually did its job and found the co-retrieval pair.
    assert stats["sessions_scanned"] >= 1
    assert stats["pairs_found"] >= 1


def test_hebbian_pass_naive_recalled_at_far_in_past_still_survives(brain):
    """Companion test: confirms the fix doesn't just work by accident for
    same-day timestamps (where the aware/naive delta happens to stay small
    enough that some other part of the code masks the bug).

    Sets a recall time 45 days in the past -- outside run_hebbian_pass's
    30-day lookback cutoff. That means this memory should be filtered out
    entirely by the cutoff query, BEFORE the aware/naive comparison ever
    happens. This test is really checking two things at once: (1) the
    30-day cutoff logic itself works correctly, and (2) reaching the
    `assert` at all -- rather than raising -- confirms nothing upstream of
    the cutoff (like the initial query or session-grouping logic) hits a
    separate aware/naive crash on its own.
    """
    from datetime import timedelta

    mem_a = brain.remember("Old recalled memory", category="lesson")
    old_recall = datetime.now() - timedelta(days=45)
    _simulate_recall(brain, mem_a, old_recall)

    db = brain._get_conn()
    stats = run_hebbian_pass(db, now=datetime.now())

    # Outside the 30-day cutoff -> filtered out before any co-retrieval
    # session could form, so we expect zero sessions here, not one.
    assert stats["sessions_scanned"] == 0
