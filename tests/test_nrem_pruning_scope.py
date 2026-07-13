"""Regression coverage for GPT's finding #1 (independent cold audit,
2026-07-12): run_nrem_phase's edge-pruning step deleted ANY knowledge_edge
below the weight threshold, regardless of relation_type or staleness --
hand-authored, causal, semantic, or imported edges could be deleted purely
because their weight happened to be low, which is destructive and outside
NREM's actual job (cleaning up dead co-retrieval associations specifically).

Fix restricts pruning to relation_type='co_referenced' (the type
run_hebbian_pass itself owns/creates/decays) AND requires the edge not
have been reinforced in NREM_PRUNE_STALE_DAYS days.
"""
from datetime import datetime, timedelta

from agentmemory.dream import run_nrem_phase


def _insert_edge(brain, relation_type, weight, last_reinforced_at=None, created_at=None):
    db = brain._get_conn()
    now_sql = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    cur = db.execute(
        "INSERT INTO knowledge_edges "
        "(source_table, source_id, target_table, target_id, relation_type, "
        " weight, agent_id, created_at, last_reinforced_at) "
        "VALUES ('memories', 1, 'memories', 2, ?, ?, 'tester', ?, ?)",
        (
            relation_type,
            weight,
            created_at or now_sql,
            last_reinforced_at,
        ),
    )
    db.commit()
    return cur.lastrowid


def test_low_weight_non_co_referenced_edges_survive_pruning(brain):
    """The direct reproduction of GPT's finding #1: a hand-authored/causal/
    semantic edge with weight far below the prune threshold must survive,
    because pruning is NREM's job for co_referenced edges only.
    """
    stale = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S")
    causal_id = _insert_edge(brain, "direct_cause", 0.01, last_reinforced_at=stale)
    semantic_id = _insert_edge(brain, "relates_to", 0.01, last_reinforced_at=stale)
    hand_authored_id = _insert_edge(brain, "manually_linked", 0.01, last_reinforced_at=stale)

    run_nrem_phase(brain._get_conn(), agent_id="tester")

    db = brain._get_conn()
    surviving_ids = {
        row["id"] for row in db.execute("SELECT id FROM knowledge_edges").fetchall()
    }
    assert causal_id in surviving_ids, "direct_cause edge was wrongly pruned by NREM"
    assert semantic_id in surviving_ids, "relates_to edge was wrongly pruned by NREM"
    assert hand_authored_id in surviving_ids, "manually_linked edge was wrongly pruned by NREM"


def test_low_weight_stale_co_referenced_edge_is_pruned(brain):
    """Companion test: confirms the fix doesn't just disable pruning
    entirely -- a genuinely dead, stale, NREM-owned edge should still go.
    """
    stale = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S")
    dead_edge_id = _insert_edge(brain, "co_referenced", 0.01, last_reinforced_at=stale)

    run_nrem_phase(brain._get_conn(), agent_id="tester")

    db = brain._get_conn()
    surviving_ids = {
        row["id"] for row in db.execute("SELECT id FROM knowledge_edges").fetchall()
    }
    assert dead_edge_id not in surviving_ids, "stale, dead co_referenced edge should have been pruned"


def test_low_weight_recently_reinforced_co_referenced_edge_survives(brain):
    """A co_referenced edge that's low-weight but was JUST reinforced
    (within NREM_PRUNE_STALE_DAYS) should not be pruned -- staleness is a
    real, separate condition from low weight, not implied by it alone.
    """
    recent = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    recent_edge_id = _insert_edge(brain, "co_referenced", 0.01, last_reinforced_at=recent)

    run_nrem_phase(brain._get_conn(), agent_id="tester")

    db = brain._get_conn()
    surviving_ids = {
        row["id"] for row in db.execute("SELECT id FROM knowledge_edges").fetchall()
    }
    assert recent_edge_id in surviving_ids, "recently-reinforced co_referenced edge was wrongly pruned"
