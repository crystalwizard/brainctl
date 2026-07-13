"""Regression coverage for two of GPT's findings from the independent cold
audit (2026-07-12), finding #9: REM isolated-bridge selection used
`_cosine()` with `zip()`, silently truncating mismatched-dimension vectors
instead of rejecting them, and its connected-candidate query had `LIMIT 500`
with no `ORDER BY`, making which 500 candidates participated
non-deterministic.
"""
import struct
from datetime import datetime

from agentmemory.dream import _cosine


def _pack(vec):
    return struct.pack(f"{len(vec)}f", *vec)


def test_cosine_rejects_mismatched_vector_dimensions():
    """A 3-dim and a 4-dim vector must not be silently compared over their
    shared 3-element prefix -- that produces a mathematically meaningless
    score that could pass a similarity threshold by accident.
    """
    a = [1.0, 0.0, 0.0]
    b = [1.0, 0.0, 0.0, 5.0]  # would look identical to `a` under zip-truncation
    assert _cosine(a, b) == 0.0


def test_cosine_still_works_correctly_for_matched_dimensions():
    """Companion test: confirms the dimension check doesn't break the
    normal case."""
    a = [1.0, 0.0]
    b = [1.0, 0.0]
    assert abs(_cosine(a, b) - 1.0) < 1e-9

    orthogonal = [0.0, 1.0]
    assert abs(_cosine(a, orthogonal)) < 1e-9


def test_connected_candidate_query_is_deterministically_ordered(brain):
    """Direct check that the connected-candidate query (as used inside
    run_rem_phase's isolated-bridge-discovery step) is fully ordered, not
    relying on SQLite's unspecified default row order. Builds several
    connected memories (each with a real knowledge_edge and a fake
    embedding) and confirms two independent executions of the same query
    return rows in identical order.
    """
    db = brain._get_conn()
    now_sql = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    ids = []
    for i in range(8):
        mem_id = brain.remember(f"Connected memory {i}", category="lesson")
        ids.append(mem_id)
        db.execute(
            "INSERT INTO embeddings (source_table, source_id, model, dimensions, vector) "
            "VALUES ('memories', ?, 'test-model', 4, ?)",
            (mem_id, _pack([1.0, 0.0, 0.0, 0.0])),
        )
    # Give each memory a real knowledge_edge to a neighbor so the
    # connected-candidate JOIN picks them up (matches run_rem_phase's own
    # WHERE/JOIN shape).
    for i in range(len(ids) - 1):
        db.execute(
            "INSERT INTO knowledge_edges "
            "(source_table, source_id, target_table, target_id, relation_type, weight, created_at) "
            "VALUES ('memories', ?, 'memories', ?, 'co_referenced', 0.5, ?)",
            (ids[i], ids[i + 1], now_sql),
        )
    db.commit()

    query = """
        SELECT DISTINCT m.id, m.content, m.scope, e.vector, e.dimensions
        FROM memories m
        JOIN knowledge_edges ke
          ON (ke.source_table='memories' AND ke.source_id=m.id)
          OR (ke.target_table='memories' AND ke.target_id=m.id)
        JOIN embeddings e
          ON e.source_table='memories' AND e.source_id=m.id
        WHERE m.retired_at IS NULL
        ORDER BY m.id ASC
        LIMIT 500
        """
    first_run = [row["id"] for row in db.execute(query).fetchall()]
    second_run = [row["id"] for row in db.execute(query).fetchall()]

    assert first_run == second_run
    assert first_run == sorted(first_run), "candidate order was not ascending by id"
