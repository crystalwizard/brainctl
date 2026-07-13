"""Regression coverage for two more UTC/local mismatches found during the
THE-65 writer inventory (same bug family as labile_until, cluster 2 --
a UTC-written value compared against a local `now`), fixed as part of
cluster 3's timestamp normalization pass.

Both next_review_at and epochs.started_at/ended_at are written elsewhere in
this codebase using SQLite's own UTC `strftime(..., 'now', ...)`. The two
functions fixed here were each separately comparing against Python's local,
naive `datetime.now()` instead -- same bug shape as labile_until, different
columns, different functions, found by the original writer inventory as
"same naive/aware inconsistency risk, but a string comparison won't raise
TypeError, it'll silently misorder instead."
"""
from datetime import datetime, timedelta, timezone

from agentmemory.hippocampus import assign_epoch, process_due_reviews


def test_process_due_reviews_recognizes_a_review_due_in_utc_terms(brain):
    """A next_review_at that has passed in real UTC terms must be picked up,
    even though (on this UTC-behind machine) it would still read as "in the
    future" relative to local wall-clock time -- the exact mirror of the
    labile_until bug, one column over.
    """
    mem_id = brain.remember("Memory due for spaced review", category="lesson")
    db = brain._get_conn()

    now_utc = datetime.now().astimezone(timezone.utc)
    due_one_minute_ago_utc = (now_utc - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    db.execute(
        "UPDATE memories SET next_review_at = ? WHERE id = ?",
        (due_one_minute_ago_utc, mem_id),
    )
    db.commit()

    stats = process_due_reviews(db)

    assert stats["reviewed"] == 1, (
        "a next_review_at that has already passed in real UTC terms was not "
        "picked up -- UTC/local comparison mismatch"
    )


def test_process_due_reviews_leaves_a_not_yet_due_review_alone(brain):
    """Companion test: a next_review_at genuinely still in the future (UTC)
    must not be picked up early.
    """
    mem_id = brain.remember("Memory not yet due for review", category="lesson")
    db = brain._get_conn()

    now_utc = datetime.now().astimezone(timezone.utc)
    due_in_one_hour_utc = (now_utc + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    db.execute(
        "UPDATE memories SET next_review_at = ? WHERE id = ?",
        (due_in_one_hour_utc, mem_id),
    )
    db.commit()

    stats = process_due_reviews(db)

    assert stats["reviewed"] == 0


def test_assign_epoch_default_timestamp_matches_utc_written_epoch_bounds(brain):
    """assign_epoch(ts=None) must find an epoch whose UTC-anchored
    started_at/ended_at genuinely cover the current instant, even though
    (pre-fix, on this UTC-behind machine) local `datetime.now()` would read
    as *before* the epoch's UTC-anchored started_at and miss the match.
    """
    db = brain._get_conn()
    now_utc = datetime.now().astimezone(timezone.utc)
    started_at = (now_utc - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    db.execute(
        "INSERT INTO epochs (name, description, started_at, ended_at) "
        "VALUES ('Test epoch', 'covers now', ?, NULL)",
        (started_at,),
    )
    db.commit()

    epoch_id = assign_epoch(db)  # ts=None -- exercises the default branch

    row = db.execute(
        "SELECT id FROM epochs WHERE name = 'Test epoch'"
    ).fetchone()
    assert epoch_id == row["id"], (
        "assign_epoch(ts=None) failed to match the UTC-anchored epoch that "
        "genuinely covers the current instant -- UTC/local comparison mismatch"
    )
