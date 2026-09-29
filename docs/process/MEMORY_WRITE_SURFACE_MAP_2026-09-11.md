# Memory write-surface map (BCTL backlog item: "~114 write call sites")

Date: 2026-09-11/12. Author: Claude. Not gated on Ari's REV9 -- independent
backlog work picked up while REV9 review of `ada259d` is pending.

## Method

`grep -rniE "INSERT INTO memories\b|UPDATE memories\b|DELETE FROM memories\b" src/agentmemory/ --include="*.py"`,
excluding test files, gave exactly 114 matches -- confirming the backlog
item's own count was accurate, not a stale estimate. Each site was then
categorized by whether its SET/INSERT columns intersect the FTS-trigger's
watched column set: `content, category, tags, indexed, retired_at` (the
exact list `memories_fts_update_insert`/`memories_fts_update_delete` scope
to, per `init_schema.sql`).

## Breakdown by file

| File | Sites |
|---|---:|
| hippocampus.py | 35 |
| _impl.py | 32 |
| mcp_tools_trust.py | 6 |
| procedural.py | 5 |
| mcp_tools_consolidation.py | 5 |
| mcp_server.py | 5 |
| brain.py | 4 |
| merge.py | 3 |
| mcp_tools_temporal.py | 2 |
| mcp_tools_neuro.py | 2 |
| mcp_tools_knowledge.py | 2 |
| mcp_tools_immunity.py | 2 |
| trust.py, mcp_tools_tom.py, mcp_tools_temporal_abstraction.py, mcp_tools_reasoning.py, mcp_tools_meb.py, mcp_tools_dmem.py, mcp_tools_allostatic.py, lib/quantum_retrieval.py, lib/belief_revision.py, integrations/crewai.py, dream.py | 1 each |

## Breakdown by operation

- UPDATE: 91
- INSERT: 23
- DELETE: 0 -- confirmed the codebase uses soft-delete (`retired_at`)
  exclusively; no site does a real `DELETE FROM memories`.

## Breakdown by FTS-trigger relevance

- **47 sites** touch at least one FTS-watched column (content/category/tags/
  indexed/retired_at) -- these correctly fire the scoped update trigger and
  are the ones this whole REV1-8 fix branch exists to protect.
- **51 sites** touch only non-watched metadata (confidence, recalled_count,
  trust_score, replay_priority, epoch_id, q_value, encoding_*, timestamps,
  etc.) -- these correctly do NOT fire the FTS trigger, which is the actual
  point of #166/167's column-scoping (retrieval-practice bookkeeping
  shouldn't churn the index). Spot-checked hippocampus.py:1202 and :2057
  (both `recalled_count`/`last_recalled_at` only) -- healthy as designed.
- **16 sites** used dynamic SQL construction (f-strings, `.format()`,
  runtime-built SET clauses) and needed manual inspection rather than
  regex column-matching. All 16 individually reviewed; results folded
  into the two counts above except for the one real finding below.

## Real finding: `retracted_at` is not eligibility-filtered by search

Not a hypothesis -- confirmed by direct comparison of two call sites.

- `_impl.py`'s trust-propagation logic (`decay_trust`/derived-memory
  traversal, e.g. line ~4086) explicitly filters
  `WHERE retired_at IS NULL AND retracted_at IS NULL` -- treating a
  retracted memory as excluded, same as a retired one, for trust-graph
  purposes.
- `mcp_server.py`'s `tool_memory_search` -- the actual user-facing search
  path -- filters only `retired_at IS NULL AND indexed = 1`. It never
  references `retracted_at` at all (confirmed: zero matches for that
  column name in the whole file).
- Retraction (`_impl.py` ~3856-3858) sets `retracted_at`, a
  `retraction_reason`, and forces `trust_score = 0.0` -- a real,
  deliberate "this content should no longer be trusted" action, distinct
  from ordinary retirement.

**Net effect: a retracted memory (trust_score forced to 0.0, explicitly
marked invalid) remains fully returnable by ordinary `tool_memory_search`,
indistinguishable from a fully-trusted live memory in the result set.**
Whether this is a real defect or intentional (retraction as an epistemic
correction that should stay discoverable/auditable rather than hidden,
the way a correction note doesn't erase the original claim) is a real
design question, not something to fix blind. Flagging for review rather
than patching -- this needs a decision about what retraction is *for*,
not just a WHERE-clause addition.

## What this does and doesn't establish

This map answers "where are the write sites and do they interact with the
FTS trigger correctly" -- it does not re-audit every non-FTS write for its
own correctness (that's a much larger task the backlog item itself flagged
as out of scope for a "wrap the obvious tools" fix). The retraction finding
above is the one thing that fell out of this specific pass worth a decision.
