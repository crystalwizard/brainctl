# brainctl tool surface v2 — migration guide

**Date:** 2026-05-20
**Scope:** Hard cutover. All v1 named tools listed below are no longer
visible in `mcp_server.list_tools()`. Their underlying Python
functions remain callable internally for trivial rollback, but agents
must migrate to the v2 dispatchers.

---

## Why

- **Harness limits.** Many MCP harnesses cap visible tools at ~100.
- **Token budget.** ~370 tools × ~150 tokens of description = ~50k
  tokens of tool schema in every system prompt. v2 cuts that to ~12k.
- **Redundancy.** The 16 brain regions shipped during the 2026-05-20
  overnight chain duplicated the shape of 11 already-shipped regions
  (BG, cerebellum, thalamus, amygdala, etc.). Each had a `*_status`,
  `*_fire`, `*_register`, `*_history`, `*_set`. The differences were
  payload-level, not tool-shape-level.

After v2 the visible surface is **100 tools** even though **370 are
still registered internally**. The 270-tool delta is hidden behind 35
action-discriminated dispatchers.

---

## Migration tables

### Subsystem dispatchers

For 27 brain subsystems — call:

| Old call | New call |
|---|---|
| `lc_status(agent_id=X)` | `subsystem_status(name="lc", agent_id=X)` |
| `lc_fire(trigger_name=X, surprise_magnitude=Y)` | `subsystem_emit(name="lc", action="fire", payload={trigger_name: X, surprise_magnitude: Y})` |
| `lc_register_trigger(name=X, ...)` | `subsystem_register(name="lc", kind="trigger", payload={name: X, ...})` |
| `lc_signal_history(limit=N, since=T)` | `subsystem_history(name="lc", filters={limit: N, since: T})` |
| `lc_set_mode(mode="tonic_high")` | `subsystem_configure(name="lc", field="set_mode", payload={mode: "tonic_high"})` |

Same pattern applies for: `nb`, `aras`, `habenula`, `ca1`,
`workspace_bw`, `connectome`, `sleep`, `vta`, `septum`, `raphe`,
`memory_aging`, `claustrum`, `colliculi`, `mammillary`, `olfactory`,
`bg`, `cerebellum`, `thalamus`, `amygdala`, `hippocampus`, `acc`,
`dmn`, `drives`, `insula`, `pfc`, `entorhinal`.

Use `subsystem_list()` to discover all valid subsystem names + their
layer, then `subsystem_list_actions(name=X)` for valid actions per
subsystem.

### Topic dispatchers

| Old call | New call |
|---|---|
| `belief_collapse(...)` | `belief(action="collapse", payload={...})` |
| `belief_get(...)` | `belief(action="get", payload={...})` |
| `belief_set(...)` | `belief(action="set", payload={...})` |
| `belief_merge(...)` | `belief(action="merge", payload={...})` |
| `tom_belief_set(...)` | `tom(action="belief_set", payload={...})` |
| `tom_status(...)` | `tom(action="status", payload={...})` |
| `trust_calibrate(...)` | `trust(action="calibrate", payload={...})` |
| `trust_show(...)` | `trust(action="show", payload={...})` |
| `reflexion_write(...)` | `reflexion(action="write", payload={...})` |
| `reflexion_query(...)` | `reflexion(action="query", payload={...})` |
| `gaps_scan(...)` | `gaps(action="scan", payload={...})` |
| `federated_search(...)` | `federated(action="search", payload={...})` |
| `world_predict(...)` | `world(action="predict", payload={...})` |
| `workspace_broadcast(...)` | `workspace(action="broadcast", payload={...})` |
| `temporal_chain(...)` | `temporal(action="chain", payload={...})` |
| `consolidation_run(...)` | `consolidation(action="run", payload={...})` |
| `expertise_build(...)` | `expertise(action="build", payload={...})` |
| `neuro_set(...)` | `neuro(action="set", payload={...})` |
| `quarantine_review(...)` | `quarantine(action="review", payload={...})` |
| `epoch_create(...)` | `epoch(action="create", payload={...})` |
| `usage_summary(...)` | `usage(action="summary", payload={...})` |
| `schedule_set(...)` | `schedule(action="set", payload={...})` |
| `task_add(...)` | `task(action="add", payload={...})` |
| `policy_add(...)` | `policy(action="add", payload={...})` |
| `meb_tail(...)` | `meb(action="tail", payload={...})` |

### Admin dispatchers

| Old call | New call |
|---|---|
| `entity_merge(...)` | `entity_admin(action="merge", payload={...})` |
| `entity_compile(...)` | `entity_admin(action="compile", payload={...})` |
| `entity_duplicates_scan(...)` | `entity_admin(action="duplicates_scan", payload={...})` |
| `memory_calibration(...)` | `memory_admin(action="calibration", payload={...})` |
| `replay_boost(...)` | `memory_admin(action="replay_boost", payload={...})` |
| `replay_queue(...)` | `memory_admin(action="replay_queue", payload={...})` |
| `attention_snapshot(...)` | `memory_admin(action="attention_snapshot", payload={...})` |
| `hot_memories(...)` | `memory_admin(action="hot", payload={...})` |
| `cold_memories(...)` | `memory_admin(action="cold", payload={...})` |
| `memory_pii(...)` | `memory_admin(action="pii", payload={...})` |
| `agent_list(...)` | `agent_admin(action="list", payload={...})` |
| `agent_activity(...)` | `agent_admin(action="activity", payload={...})` |
| `handoff_consume(...)` | `handoff_admin(action="consume", payload={...})` |
| `handoff_pin(...)` | `handoff_admin(action="pin", payload={...})` |
| `trigger_list(...)` | `trigger_admin(action="list", payload={...})` |
| `trigger_update(...)` | `trigger_admin(action="update", payload={...})` |

### Tools that stayed the same

These daily-use tools kept their v1 names:

- `memory_add`, `memory_search`, `vsearch`, `search`, `search_patterns`
- `event_add`, `event_search`, `event_link`
- `decision_add`
- `entity_create`, `entity_get`, `entity_search`, `entity_observe`, `entity_relate`
- `procedure_add`, `procedure_get`, `procedure_list`, `procedure_search`
- `handoff_add`, `handoff_latest`
- `trigger_create`, `trigger_check`
- `agent_orient`, `brainctl_wrapup`, `agent_register`
- `affect_classify`, `affect_log`, `affect_check`, `affect_monitor`
- `reason`, `infer`, `infer_pretask`, `infer_gapfill`, `think`
- `reconsolidate`, `reconsolidation_check`, `promote`
- `free_energy_check`, `retirement_analysis`, `retrieval_effectiveness`
- `allostatic_prime`, `demand_forecast`
- `pagerank`, `health`, `stats`, `validate`, `lint`, `backup`
- `weights`, `whosknows`, `dream_cycle`, `telemetry`, `write_gate_stats`
- `budget_set`, `budget_status`
- `wallet_create`, `wallet_show`
- `resolve_conflict`, `merge_status`, `merge_execute`
- `abstract_summarize`, `zoom_in`, `zoom_out`
- `push`, `push_report`

---

## Rollback

If a downstream agent breaks and you need v1 names back, the
consolidation can be undone in one of three ways:

1. **Soft rollback (preferred):** Remove the `DEPRECATED_TOOL_NAMES`
   filter in `mcp_server.py:list_tools`. The v1 tool entries return to
   `tools/list` immediately. The v2 dispatchers stay (no harm), and
   the tool count goes back to ~370.
2. **Hard rollback:** `git revert` the consolidation commit. Removes
   the dispatcher module and the filter together.
3. **Per-tool exception:** Edit `DEPRECATED_TOOL_NAMES` in
   `mcp_tools_consolidated.py` to exclude specific tool names; those
   become visible again while the rest of the consolidation stays.

The underlying Python tool functions in `mcp_tools_*.py` are
**untouched**. Every deprecated v1 tool function is still in the
global DISPATCH dict and callable. The cutover is a visibility filter,
not a deletion.

---

## What to do as an agent author

1. Update your tool-list reference. The new visible surface is in
   `MCP_SERVER.md` ("Available Tools (100)").
2. Find any v1 tool name your agent calls. Look it up in this guide
   for the v2 equivalent.
3. Replace direct calls with the dispatcher form. The payload dict is
   forwarded as kwargs to the underlying function — same arg shape, just
   wrapped in `payload={...}`.
4. For discovery: `subsystem_list()` + `subsystem_list_actions(name=X)`
   tell you everything valid for any subsystem at runtime.

---

## Measured impact

| | v1 (main) | v2 (consolidated) |
|---|---|---|
| Visible tool count | 260 | 100 |
| Total registered (still callable internally) | 260 | 370 |
| Tool description tokens in system prompt | ~40k | ~12k |
| `list_tools()` response time | <1ms | <1ms |
| Cold-start import time | ~340ms | ~340ms |
| `tests/bench/run --check` retrieval quality | P@1=0.60 / P@5=0.18 / Recall@5=0.51 | P@1=0.60 / P@5=0.18 / Recall@5=0.51 (zero delta) |

The bench harness confirms no retrieval regression.

## See also

- `docs/proposals/brain_region_coverage.md` — what brain regions exist
- `docs/proposals/*.md` — per-region design proposals shipped 2026-05-20
- `research/autonomous-research-avenues-2026-05-20.md` — what's next
- `CHANGELOG.md` — the [Unreleased] entry covers the full overnight + this consolidation
