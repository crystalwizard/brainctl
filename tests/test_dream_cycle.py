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
from datetime import datetime, timedelta, timezone

import pytest

from agentmemory.dream import should_run_dream_cycle
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


# ============================================================================
# THE-65 Cluster 6, Group 1: dream.py's own writer/reader timestamp bug.
#
# Written by Claude, 2026-07-13, per GPT's GO-for-Group-1 review on THE-65
# (comment 9b51af57). Separate bug from the #168 crash above -- this one
# doesn't raise, it silently produces a WRONG answer, which is worse: the
# real NREM/REM/Insight orchestrator lives in dream.py (not hippocampus.py --
# a real gap in the original 2026-07-12 inventory, found while building the
# Cluster 6 writer inventory), and its idle-trigger logic
# (`should_run_dream_cycle`) had its own bespoke, narrower timestamp handling
# than hippocampus.py's `parse_ts()`.
#
# THE BUG: `should_run_dream_cycle` parsed `events.created_at` with an inline
# two-format `strptime` after slicing the string to 19 characters -- which
# silently drops any trailing `Z` or explicit offset instead of parsing it.
# Most real events are written via `brain.log()` -> `_utc_now_iso()`, an
# aware, Z-suffixed UTC string. On a UTC-7 machine (this one -- Prescott, AZ,
# no DST), stripping the `Z` and treating those digits as *local* time makes
# the parsed instant look ~7 hours in the future relative to local
# `datetime.now()`. The existing `max(0.0, raw)` floor -- added to guard
# against exactly this kind of skew -- then silently clamps the resulting
# negative delta to 0.0, which reads as "no idle time has passed" even when
# real idle time has. Net effect: the idle-based dream-cycle trigger was
# silently dead whenever the most recent event used the modern writer --
# only the memory-count trigger could ever fire. Reproduced live before any
# fix (THE-65 inventory comment): -25199.94s raw, 0.0s after clamp.
#
# THE FIX: dream.py now reuses hippocampus.py's already-tested `parse_ts()`
# instead of its own narrower parser, and its own `_now_sql()` writer is
# aliased to the shared `now_iso()` helper (GPT's explicit instruction: don't
# invent a second canonical-timestamp helper).
#
# TWO ADDITIONAL CORRECTIONS FROM GPT'S REVIEW, both covered below:
#   1. `SELECT max(created_at) FROM events` picks "most recent event" by
#      lexical string comparison, which disagrees with real insertion order
#      once the table has mixed naive/Z-suffixed rows. Fixed to
#      `ORDER BY id DESC LIMIT 1` (the table's real AUTOINCREMENT order).
#   2. The new-memory trigger's `created_at > last_cycle_at` SQL comparison
#      is also lexical. It happens to work correctly for the two forms real
#      writers actually produce (legacy naive, canonical Z) because ISO-8601
#      dates sort correctly as strings when the date/time digits differ --
#      but it is NOT safe against an arbitrary explicit-offset timestamp,
#      which no current writer produces but which parse_ts() itself accepts.
#      Per GPT's explicit instruction ("do not claim all trigger timestamp
#      logic is normalized while it remains lexical"), this is tested and
#      documented as a known, narrow, currently-inert limitation -- not
#      silently left unexamined, and not oversold as fixed.
# ============================================================================


def _insert_event(brain, created_at: str, summary: str = "test event") -> int:
    """Insert an events row with an exact, caller-controlled created_at string.

    Bypasses brain.log() (which always writes the modern Z-suffixed form)
    so tests can construct legacy-naive, canonical-Z, and explicit-offset
    rows side by side -- exactly the mixed-format scenario these tests are
    checking dream.py's reader logic against.
    """
    db = brain._get_conn()
    cur = db.execute(
        "INSERT INTO events (agent_id, event_type, summary, created_at) "
        "VALUES (?, 'observation', ?, ?)",
        (brain.agent_id, summary, created_at),
    )
    db.commit()
    return cur.lastrowid


def _set_last_cycle_at(brain, value: str) -> None:
    """Write agent_state's last_dream_cycle_at exactly the way
    mark_dream_cycle_complete does (json.dumps'd value, read back via
    .strip('"') in should_run_dream_cycle) -- so tests control the stored
    value directly without depending on real wall-clock time.
    """
    import json as _json

    db = brain._get_conn()
    # agent_state.agent_id has a real FK to agents(id); production code
    # goes through _ensure_agent(db, 'hippocampus') first (inside
    # mark_dream_cycle_complete). Do the same here rather than assume the
    # row exists.
    db.execute(
        "INSERT OR IGNORE INTO agents (id, display_name, agent_type, status, created_at, updated_at) "
        "VALUES ('hippocampus', 'hippocampus', 'system', 'active', ?, ?)",
        (value, value),
    )
    db.execute(
        "INSERT INTO agent_state (agent_id, key, value, updated_at) "
        "VALUES ('hippocampus', 'last_dream_cycle_at', ?, ?) "
        "ON CONFLICT(agent_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (_json.dumps(value), value),
    )
    db.commit()


def test_idle_trigger_survives_aware_zulu_event_timestamp(brain):
    """THE actual reproduction of the live bug found in the Cluster 6 inventory.

    Writes one real event the way production code actually does it --
    brain.log() -> aware, Z-suffixed UTC -- timed far enough in the past
    (well past the idle threshold) using REAL wall-clock time, exactly like
    the original #168 test above uses real `datetime.now()` rather than a
    mocked clock. This machine is reliably UTC-7 (Arizona, no DST), so this
    reproduces the actual discovered failure mode deterministically here,
    the same way the live bug was actually found.

    Pre-fix: the inline 19-char-sliced naive parser drops the trailing Z,
    computes a large negative raw idle delta against local time, and the
    existing `max(0.0, raw)` clamp turns that into 0.0 -- `should_run` stays
    False even though a real idle gap passed.

    Post-fix: parse_ts() correctly treats the Z-suffixed string as aware
    UTC, arithmetic is done in UTC throughout, and the real elapsed idle
    time is reported accurately.
    """
    idle_threshold = 60  # seconds

    # Real event, real aware Z-suffixed writer, timed well before the
    # threshold using real wall-clock time (no mocked `now`).
    brain.log("something happened a while ago", event_type="observation")

    # Directly backdate that event's created_at to be unambiguously past
    # the idle threshold, still via the same real production writer format
    # (aware UTC, Z-suffixed) -- isolates "is this parsed correctly" from
    # "did enough real wall-clock time elapse during the test run".
    db = brain._get_conn()
    backdated = (
        (datetime.now(timezone.utc) - timedelta(seconds=idle_threshold + 120))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    db.execute("UPDATE events SET created_at = ? WHERE agent_id = ?", (backdated, brain.agent_id))
    db.commit()

    decision = should_run_dream_cycle(db, idle_seconds=idle_threshold, memory_threshold=10_000)

    assert decision["idle_seconds"] is not None
    # The whole bug was this silently reading as ~0.0 instead of ~180.
    assert decision["idle_seconds"] >= idle_threshold
    assert decision["should_run"] is True
    assert decision["reason"].startswith("idle_")


def test_idle_trigger_uses_insertion_order_not_lexical_max(brain):
    """GPT's correction 1: SELECT max(created_at) disagrees with real
    insertion order under mixed timestamp formats.

    Row 1 is inserted first (true earliest by AUTOINCREMENT id) but is
    given a lexically LARGER created_at string. Row 2 is inserted second
    (true most recent by id) but is given a lexically SMALLER string.
    `max(created_at)` would wrongly select row 1 as "most recent"; the
    fixed `ORDER BY id DESC LIMIT 1` correctly selects row 2.

    Row 2's timestamp is set far enough in the past that the test can tell
    the two outcomes apart just by observing should_run/idle_seconds on the
    public function, without reaching into internals. Row 1 is deliberately
    timestamped as "right now" (real wall-clock, Z-suffixed) rather than a
    fixed string -- so if the old max(created_at) logic wins, idle_seconds
    reads as ~0 regardless of what the actual test-run time happens to be,
    genuinely distinguishing "picked row 1" from "picked row 2" instead of
    relying on incidental arithmetic between two fixed dates.
    """
    right_now = (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    _insert_event(brain, right_now, "inserted first, lexically larger (real 'now')")
    _insert_event(brain, "2026-01-05T00:00:00", "inserted second (real most recent), lexically smaller")

    db = brain._get_conn()
    decision = should_run_dream_cycle(db, idle_seconds=60, memory_threshold=10_000)

    # If max(created_at) won, the "most recent" row would be the first
    # (2026-07-13, recent) and idle_seconds would be small/zero -- no
    # trigger. If insertion order wins (the fix), the real most-recent row
    # is the ancient 2026-01-05 one, so idle_seconds is huge and it fires.
    assert decision["should_run"] is True
    assert decision["reason"].startswith("idle_")
    assert decision["idle_seconds"] > 3600  # unambiguously not "recent"


def test_new_memory_trigger_mixed_naive_and_zulu_at_boundary(brain):
    """GPT's correction 2: created_at > last_cycle_at is still a lexical
    SQL comparison. Prove it behaves correctly for the two forms real
    writers actually produce (legacy naive, canonical Z) around the
    last-cycle boundary, including the same-instant edge case.
    """
    _set_last_cycle_at(brain, "2026-07-10T12:00:00Z")

    db = brain._get_conn()

    # Before the cutoff (naive) -- must NOT count as new.
    db.execute(
        "INSERT INTO memories (agent_id, category, scope, content, confidence, created_at, updated_at) "
        "VALUES (?, 'lesson', 'global', 'before cutoff, naive', 1.0, ?, ?)",
        (brain.agent_id, "2026-07-09T12:00:00", "2026-07-09T12:00:00"),
    )
    # Same instant as the cutoff, naive form (no Z) -- naive-as-prefix sorts
    # BEFORE the Z-suffixed cutoff string lexically, so this correctly does
    # NOT count as strictly-after (matches the `>`, not `>=`, semantics).
    db.execute(
        "INSERT INTO memories (agent_id, category, scope, content, confidence, created_at, updated_at) "
        "VALUES (?, 'lesson', 'global', 'same instant, naive', 1.0, ?, ?)",
        (brain.agent_id, "2026-07-10T12:00:00", "2026-07-10T12:00:00"),
    )
    # After the cutoff, canonical Z -- must count.
    db.execute(
        "INSERT INTO memories (agent_id, category, scope, content, confidence, created_at, updated_at) "
        "VALUES (?, 'lesson', 'global', 'after cutoff, zulu', 1.0, ?, ?)",
        (brain.agent_id, "2026-07-11T12:00:00Z", "2026-07-11T12:00:00Z"),
    )
    # After the cutoff, naive form -- must also count (date digits alone
    # already put it after, regardless of the trailing Z difference).
    db.execute(
        "INSERT INTO memories (agent_id, category, scope, content, confidence, created_at, updated_at) "
        "VALUES (?, 'lesson', 'global', 'after cutoff, naive', 1.0, ?, ?)",
        (brain.agent_id, "2026-07-11T12:00:00", "2026-07-11T12:00:00"),
    )
    db.commit()

    decision = should_run_dream_cycle(db, idle_seconds=10_000_000, memory_threshold=2)

    assert decision["new_memories_since_last"] == 2
    assert decision["should_run"] is True
    assert decision["reason"].startswith("new_memories_2")


def test_new_memory_trigger_known_limitation_with_explicit_offset(brain):
    """Documents, rather than hides, a real edge case GPT flagged: the
    lexical `created_at > last_cycle_at` comparison is not safe against an
    arbitrary explicit-offset timestamp, even though parse_ts() itself
    accepts and correctly converts one.

    No current production writer emits an explicit non-Z offset for
    memories.created_at (confirmed by inspection of every INSERT INTO
    memories site during the Cluster 6 writer inventory) -- so this is a
    real but currently inert gap, not a live bug. This test exists so the
    limitation is checked and visible, not silently unexamined, per GPT's
    instruction not to claim full normalization while any lexical
    comparison remains.
    """
    _set_last_cycle_at(brain, "2026-07-10T12:00:00Z")  # real UTC instant: July 10, 12:00 UTC

    db = brain._get_conn()
    # Real UTC instant: 20:00 - 9h = 11:00 UTC on 2026-07-10 -- BEFORE the
    # cutoff. A correct (parse_ts-based) comparison would exclude this.
    db.execute(
        "INSERT INTO memories (agent_id, category, scope, content, confidence, created_at, updated_at) "
        "VALUES (?, 'lesson', 'global', 'real UTC before cutoff, offset digits look later', 1.0, ?, ?)",
        (brain.agent_id, "2026-07-10T20:00:00+09:00", "2026-07-10T20:00:00+09:00"),
    )
    db.commit()

    decision = should_run_dream_cycle(db, idle_seconds=10_000_000, memory_threshold=1)

    # KNOWN LIMITATION, asserted explicitly rather than left unchecked: the
    # lexical comparison sees "20" > "12" in the hour position and counts
    # this row as new, even though its real UTC instant is before the
    # cutoff. If this assertion ever starts failing, it means the lexical
    # comparison has been replaced with a real parse_ts()-based one --
    # update this test (and the docstring above) to reflect the fix rather
    # than treating the new, correct behavior as a regression.
    assert decision["new_memories_since_last"] == 1
