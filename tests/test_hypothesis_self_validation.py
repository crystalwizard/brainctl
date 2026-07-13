"""Regression coverage for GPT's independent audit finding #2 (2026-07-12,
CRITICAL): a dream-generated hypothesis could be "recalled" purely by
automatic NREM replay (experience_replay didn't exclude category=
'hypothesis'), and run_dream_pass's promotion check treats any
recalled_count > 0 as evidence of meaningful recall -- so a hypothesis
could promote itself to an accepted lesson without any user or agent ever
actually retrieving or endorsing it. A self-fulfilling consolidation loop.

Fix: experience_replay now excludes category='hypothesis' memories from
its replay candidate query entirely, so automatic maintenance replay can
never increment a hypothesis's recalled_count.
"""
from datetime import datetime

from agentmemory.hippocampus import experience_replay


def test_experience_replay_never_selects_hypothesis_memories(brain):
    """The direct reproduction: a hypothesis-category memory with a high
    recalled_count/confidence (so it would otherwise be the #1 replay
    candidate) must never appear in experience_replay's output.
    """
    db = brain._get_conn()
    now_sql = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    # Insert a hypothesis memory directly with recalled_count=0 (as
    # run_dream_pass actually creates them) but very high confidence, so
    # it would sort first in experience_replay's own ORDER BY if not
    # excluded by category.
    cur = db.execute(
        "INSERT INTO memories (agent_id, category, scope, content, confidence, "
        "temporal_class, memory_type, recalled_count, created_at, updated_at) "
        "VALUES (?, 'hypothesis', 'global', 'Unvalidated dream hypothesis', "
        "0.99, 'ephemeral', 'episodic', 0, ?, ?)",
        (brain.agent_id, now_sql, now_sql),
    )
    db.commit()
    hypothesis_id = cur.lastrowid

    stats = experience_replay(db, top_k=10, now=datetime.now())

    assert hypothesis_id not in stats["ids"], (
        "a hypothesis-category memory was replayed -- it can now "
        "self-validate via automatic maintenance replay"
    )

    # Confirm its recalled_count genuinely never moved (the actual
    # mechanism the self-validation loop depends on).
    row = db.execute(
        "SELECT recalled_count FROM memories WHERE id = ?", (hypothesis_id,)
    ).fetchone()
    assert row["recalled_count"] == 0


def test_experience_replay_still_replays_ordinary_memories(brain):
    """Companion test: confirms the exclusion is scoped to
    category='hypothesis' specifically, not a general breakage of replay.
    """
    mem_id = brain.remember("An ordinary, real memory", category="lesson", confidence=0.9)
    db = brain._get_conn()

    stats = experience_replay(db, top_k=10, now=datetime.now())

    assert mem_id in stats["ids"]
