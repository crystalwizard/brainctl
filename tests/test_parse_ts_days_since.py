"""Direct unit coverage for parse_ts() and days_since() -- the THE-65
timestamp-policy core, as explicitly requested by GPT's acceptance criteria
on THE-65: "add tests for naive legacy input, Z, positive/negative offsets,
DST-adjacent values if local-time conversion is chosen, and mixed old/new
rows." (DST-adjacent local-conversion tests are not applicable here -- the
chosen policy treats legacy naive *stored* values as UTC directly, not
local-time-converted; see parse_ts's docstring for why. `now`, by contrast,
genuinely is local time when naive, and is converted -- covered by the
naive-now tests below.)
"""
from datetime import datetime, timedelta, timezone

from agentmemory.hippocampus import days_since, parse_ts


def test_parse_ts_returns_aware_utc_for_naive_legacy_input():
    dt = parse_ts("2026-07-12T18:00:00")
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(0)
    assert dt.hour == 18  # naive digits taken as UTC directly, not shifted


def test_parse_ts_returns_aware_utc_for_z_suffixed_input():
    dt = parse_ts("2026-07-12T18:00:00Z")
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(0)
    assert dt.hour == 18


def test_parse_ts_normalizes_positive_offset_to_utc():
    # 18:00 at +05:00 is 13:00 UTC
    dt = parse_ts("2026-07-12T18:00:00+05:00")
    assert dt.utcoffset() == timedelta(0)
    assert dt.hour == 13


def test_parse_ts_normalizes_negative_offset_to_utc():
    # 18:00 at -07:00 (this machine's real offset) is 01:00 UTC the next day
    dt = parse_ts("2026-07-12T18:00:00-07:00")
    assert dt.utcoffset() == timedelta(0)
    assert dt.hour == 1
    assert dt.day == 13


def test_parse_ts_none_stays_none():
    assert parse_ts(None) is None


def test_days_since_accepts_naive_now_and_aware_stored_timestamp_without_crashing():
    """The literal #168 crash shape, at the unit level: naive `now` (from a
    bare datetime.now() call) against an aware, UTC-suffixed stored value.

    Not asserting an exact day count here: naive `now` is deliberately
    local-converted (see days_since's docstring), so the precise result
    legitimately depends on this machine's real UTC offset -- e.g. on this
    UTC-7 machine, naive local 2026-07-13T01:00:00 is actually UTC
    2026-07-13T08:00:00, ~1.29 days after the stored UTC timestamp below,
    not exactly 1.0. What this test actually checks is the thing that
    matters: it doesn't raise, and the result is sane (positive, and in the
    right ballpark for "about a day," not wildly wrong).
    """
    now = datetime(2026, 7, 13, 1, 0, 0)  # naive
    result = days_since(now, "2026-07-12T01:00:00Z")  # aware, ~1 day earlier
    assert 0.5 <= result <= 2.0


def test_days_since_accepts_naive_now_and_naive_stored_timestamp():
    """Both naive -- must not crash, and (per policy) both are treated as
    UTC digits directly for the stored value, while naive `now` is
    local-converted -- so this only stays exactly 1.0 when now's local
    offset is zero. What actually matters here is that it doesn't raise,
    and produces a small, bounded, non-negative number consistent with
    "about a day," not an arbitrarily wrong one.
    """
    now = datetime(2026, 7, 13, 1, 0, 0)
    result = days_since(now, "2026-07-12T01:00:00")
    assert result >= 0.0
    assert result < 2.0  # bounded: local UTC offsets don't span more than a day


def test_days_since_accepts_aware_now_and_naive_stored_timestamp():
    now = datetime(2026, 7, 13, 1, 0, 0, tzinfo=timezone.utc)  # aware
    result = days_since(now, "2026-07-12T01:00:00")  # naive, treated as UTC
    assert abs(result - 1.0) < 0.01


def test_days_since_handles_mixed_old_and_new_rows_in_sequence():
    """Simulates a real database with rows written before and after the
    timestamp-policy fix landed -- some naive (legacy), some aware/Z-suffixed
    (post-fix writers) -- processed in the same pass with the same `now`.
    None should raise; all should produce sane, non-negative day counts.
    """
    now = datetime.now()
    legacy_naive = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S")
    modern_z = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    modern_offset = (datetime.now(timezone.utc) - timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    for ts in (legacy_naive, modern_z, modern_offset):
        result = days_since(now, ts)
        assert result >= 0.0


def test_days_since_future_timestamp_floors_to_zero_not_negative():
    now = datetime.now(timezone.utc)
    future = (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert days_since(now, future) == 0.0
