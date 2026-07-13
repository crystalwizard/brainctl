"""Regression coverage for GPT's independent audit finding #4 (2026-07-12,
CRITICAL): run_insight_phase wrote its bridge-node output directly as
category='lesson', memory_type='semantic', temporal_class='long' -- the
schema's "accepted, reviewed knowledge" shape. Graph betweenness only
proves a memory topologically bridges two communities; it does not prove
the generated prose describing that bridge is a valid lesson.

Fix writes Insight output as category='hypothesis' instead (the same
unreviewed-candidate category REM's own bisociation output already uses),
so it isn't picked up as accepted fact.

Builds a real minimal two-community graph with one bridge edge (not a
mocked graph algorithm) so this exercises the actual label-propagation and
betweenness code paths, not a stand-in.
"""
from datetime import datetime

from agentmemory.dream import run_insight_phase


def _link(db, a_id, b_id, now_sql):
    db.execute(
        "INSERT INTO knowledge_edges "
        "(source_table, source_id, target_table, target_id, relation_type, weight, created_at) "
        "VALUES ('memories', ?, 'memories', ?, 'co_referenced', 0.8, ?)",
        (a_id, b_id, now_sql),
    )


def _build_two_community_bridge_graph(brain):
    """Community A: mem 0-4 fully connected. Community B: mem 5-9 fully
    connected. One bridge edge: mem 0 -- mem 5. Larger, densely-connected
    communities (5 members each, 10 internal edges per side) than a minimal
    3+3 topology -- found empirically that a smaller graph occasionally
    collapsed to a single community depending on label-propagation's
    internal iteration order (a real, separate determinism concern in
    _graph_communities itself, out of scope for this fix -- flagging it
    rather than chasing it here). Denser communities converge reliably.
    """
    db = brain._get_conn()
    now_sql = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    ids = [brain.remember(f"Community memory {i}", category="lesson") for i in range(10)]

    community_a, community_b = ids[:5], ids[5:]
    for group in (community_a, community_b):
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                _link(db, group[i], group[j], now_sql)
    _link(db, community_a[0], community_b[0], now_sql)  # the bridge
    db.commit()
    return ids


def test_insight_writes_hypothesis_not_lesson(brain):
    ids = _build_two_community_bridge_graph(brain)
    db = brain._get_conn()

    # _graph_communities' label-propagation is seeded (seed=42) but its
    # iteration order over node collections isn't fully pinned, so it
    # occasionally converges to 1 community instead of 2 on an identical
    # graph across separate process runs -- observed directly while writing
    # this test (real, separate nondeterminism in _impl.py, out of scope
    # for this fix; not chasing it here). run_insight_phase recomputes
    # communities fresh (force=True) each call, so a bounded retry against
    # the *same* unmodified graph is legitimate here: what's under test is
    # the category this function writes when it DOES find a bridge, not
    # whether community detection itself is deterministic.
    stats = None
    for _attempt in range(5):
        stats = run_insight_phase(db, agent_id=brain.agent_id)
        if stats.get("insights_created", 0) >= 1:
            break

    assert stats.get("insights_created", 0) >= 1, (
        f"test graph did not produce any insight to check after 5 attempts "
        f"-- stats={stats!r}"
    )

    rows = db.execute(
        "SELECT category, memory_type, temporal_class FROM memories "
        "WHERE content LIKE 'Bridge insight:%'"
    ).fetchall()
    assert rows, "no bridge-insight memory was written despite insights_created > 0"
    for row in rows:
        assert row["category"] == "hypothesis", (
            f"Insight wrote category={row['category']!r} -- should be "
            "'hypothesis' (unreviewed candidate), not an accepted category"
        )
