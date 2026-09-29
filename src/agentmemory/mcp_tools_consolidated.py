"""brainctl consolidated MCP tool surface (v2).

Hard-cutover consolidation. Replaces ~150 per-subsystem tools with
~13 action-discriminated dispatchers. The underlying Python functions
in `mcp_tools_*.py` modules stay intact — this module is a routing
layer over them, looked up at runtime through each module's existing
DISPATCH dict.

Rollback: revert this file + restore the `DEPRECATED_TOOL_NAMES`
filter in mcp_server.py. Underlying functions are untouched, so the
v1 named surface returns immediately.

Design:
  - Each dispatcher resolves at call-time via the global tool-name → callable
    map (built once on first call from each module's DISPATCH dict).
  - Routing tables map (subsystem, action) → v1 tool name. Easy to inspect,
    easy to extend, no symbol attribution gambles.
  - `subsystem_list()` and `subsystem_list_actions()` are the discoverability
    surfaces — agents call them to learn the routing tables.

Author: claude (consolidation pass 2026-05-20)
"""
from __future__ import annotations

import inspect
from typing import Any

from mcp.types import Tool


# Handlers in this codebase use one of three signature shapes:
#   1. `fn(args: dict) -> dict` — extension-module `_call_*` handlers
#      (mcp_tools_lifecycle, mcp_tools_reflexion, …). Single positional dict.
#   2. `fn(**kwargs) -> dict` — `tool_*` functions in mcp_server.py and most
#      brain-region modules. Keyword arguments.
#   3. `fn()` — a small handful of zero-arg tools (stats, weights, health, …).
# `_call_by_name` introspects the signature once per call and routes to the
# right shape. Caches the choice so the inspect cost is paid once per handler.
# Key by the function object itself (not id()) because id() is recycled
# for short-lived test closures, which made tests pollute each other.
_SIG_KIND_CACHE: dict[Any, str] = {}


def _signature_kind(fn: Any) -> str:
    """Return one of "single_dict" / "kwargs" / "zero" based on fn's signature.

    Treats a single non-self positional parameter named `arguments` /
    `args` / `payload` / `params` / `kwargs_dict` as a single-dict
    handler. Everything else is kwargs-shape. Zero-arg handlers are
    detected separately.
    """
    cached = _SIG_KIND_CACHE.get(fn)
    if cached is not None:
        return cached
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        kind = "kwargs"
        try:
            _SIG_KIND_CACHE[fn] = kind
        except TypeError:
            pass
        return kind
    params = [
        p for p in sig.parameters.values()
        if p.name not in ("self", "cls")
    ]
    if not params:
        kind = "zero"
    elif (
        len(params) == 1
        and params[0].kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        )
        and params[0].default is inspect.Parameter.empty
        and params[0].name in ("arguments", "args", "payload", "params", "kwargs_dict")
    ):
        kind = "single_dict"
    else:
        kind = "kwargs"
    try:
        _SIG_KIND_CACHE[fn] = kind
    except TypeError:
        pass
    return kind


# ---------------------------------------------------------------- runtime dispatch map

_GLOBAL_DISPATCH: dict[str, Any] | None = None


def _collect_dispatch() -> dict[str, Any]:
    """Build a global tool-name → callable map from all known module
    DISPATCH dicts. Lazy + memoized."""
    global _GLOBAL_DISPATCH
    if _GLOBAL_DISPATCH is not None:
        return _GLOBAL_DISPATCH
    combined: dict[str, Any] = {}
    import importlib
    for mod_name in (
        # Tonight's modules
        "mcp_tools_locus_coeruleus", "mcp_tools_nucleus_basalis",
        "mcp_tools_aras", "mcp_tools_habenula", "mcp_tools_hippocampus_ca1",
        "mcp_tools_workspace_bandwidth", "mcp_tools_connectome",
        "mcp_tools_sleep_architecture", "mcp_tools_vta_snc",
        "mcp_tools_septum_theta", "mcp_tools_raphe",
        "mcp_tools_memory_aging", "mcp_tools_claustrum",
        "mcp_tools_colliculi", "mcp_tools_mammillary", "mcp_tools_olfactory",
        # Existing brain-region modules
        "mcp_tools_basal_ganglia", "mcp_tools_cerebellum", "mcp_tools_thalamus",
        "mcp_tools_amygdala", "mcp_tools_hippocampal_subfields", "mcp_tools_acc",
        "mcp_tools_dmn", "mcp_tools_drives", "mcp_tools_insula", "mcp_tools_pfc",
        "mcp_tools_entorhinal_grid",
        # Topic modules
        "mcp_tools_beliefs", "mcp_tools_belief_merge", "mcp_tools_tom",
        "mcp_tools_trust", "mcp_tools_reflexion", "mcp_tools_expertise",
        "mcp_tools_federation",
        # All remaining extension modules — needed for the additional
        # action-discriminated dispatchers (world, workspace, temporal, etc.)
        "mcp_tools_agents", "mcp_tools_allostatic", "mcp_tools_analytics",
        "mcp_tools_consolidation", "mcp_tools_dmem", "mcp_tools_health",
        "mcp_tools_immunity", "mcp_tools_knowledge", "mcp_tools_lifecycle",
        "mcp_tools_meb", "mcp_tools_merge", "mcp_tools_neuro",
        "mcp_tools_policy", "mcp_tools_procedural", "mcp_tools_reasoning",
        "mcp_tools_reconcile", "mcp_tools_scheduler", "mcp_tools_telemetry",
        "mcp_tools_temporal", "mcp_tools_temporal_abstraction",
        "mcp_tools_usage", "mcp_tools_workspace", "mcp_tools_world",
    ):
        try:
            mod = importlib.import_module(f"agentmemory.{mod_name}")
            combined.update(getattr(mod, "DISPATCH", {}) or {})
        except ImportError:
            continue
    # Also pull tool_* functions defined directly in mcp_server.py
    # (belief_collapse, handoff_consume/expire/pin, trigger_delete/list/update,
    # access_log_annotate, etc.). We import LATE to avoid circular deps.
    try:
        from agentmemory import mcp_server as _ms
        # mcp_server may expose a top-level DISPATCH dict in the future
        combined.update(getattr(_ms, "DISPATCH", {}) or {})
        # Walk module-level tool_* functions and key them by the canonical
        # tool name (everything after the tool_ prefix).
        for attr_name in dir(_ms):
            if attr_name.startswith("tool_"):
                fn = getattr(_ms, attr_name)
                if callable(fn):
                    tool_name = attr_name[len("tool_"):]
                    combined.setdefault(tool_name, fn)
    except ImportError:
        pass
    _GLOBAL_DISPATCH = combined
    return combined


def _call_by_name(tool_name: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    disp = _collect_dispatch()
    fn = disp.get(tool_name)
    if fn is None:
        return {"error": f"underlying tool {tool_name!r} not found in dispatch (consolidated routing miss)"}
    args = payload or {}
    kind = _signature_kind(fn)
    try:
        if kind == "single_dict":
            return fn(args)
        if kind == "zero":
            return fn()
        return fn(**args)
    except TypeError as exc:
        # Last-resort fallback: try the other shape before surfacing the error.
        # Covers handlers whose param is named idiosyncratically and got
        # misclassified as kwargs (or vice versa).
        try:
            if kind == "kwargs":
                return fn(args)
            return fn(**args)
        except TypeError:
            return {"error": f"argument mismatch calling {tool_name!r}: {exc}"}


# ---------------------------------------------------------------- routing tables

# subsystem name → v1 tool name for status
_STATUS_ROUTE: dict[str, str] = {
    # tonight's
    "lc":           "lc_status",
    "nb":           "nb_status",
    "aras":         "aras_status",
    "habenula":     "habenula_status",
    "ca1":          "ca1_status",
    "workspace_bw": "workspace_bandwidth_status",
    "connectome":   "connectome_status",
    "sleep":        "sleep_status",
    "vta":          "vta_status",
    "septum":       "septum_status",
    "raphe":        "raphe_status",
    "memory_aging": "memory_aging_status",
    "claustrum":    "claustrum_status",
    "colliculi":    "colliculi_status",
    "mammillary":   "mammillary_status",
    "olfactory":    "olfactory_status",
    # existing
    "bg":           "bg_status",
    "cerebellum":   "cerebellum_status",
    "thalamus":     "thalamus_status",
    "amygdala":     "amygdala_status",
    "hippocampus":  "hippocampus_subfields_status",
    "acc":          "acc_status",
    "dmn":          "dmn_schedule_status",
    "drives":       "drive_status",
    "insula":       "insula_state",
    "pfc":          "pfc_status",
    "entorhinal":   "entorhinal_status",
}

# (subsystem, action) → v1 tool name for emit
_EMIT_ROUTE: dict[tuple[str, str], str] = {
    # tonight's
    ("lc",           "fire"):                "lc_fire",
    ("nb",           "fire"):                "nb_fire",
    ("nb",           "attend_sector"):       "nb_attend_sector",
    ("aras",         "transition"):          "aras_transition",
    ("aras",         "drive"):               "aras_drive",
    ("habenula",     "fire"):                "habenula_fire",
    ("habenula",     "reset"):               "habenula_reset",
    ("ca1",          "compare"):             "ca1_compare",
    ("ca1",          "subiculum_output"):    "subiculum_output",
    ("workspace_bw", "admit"):               "workspace_bandwidth_admit",
    ("sleep",        "transition"):          "sleep_transition",
    ("sleep",        "advance"):             "sleep_advance",
    ("sleep",        "operation_permitted"): "sleep_operation_permitted",
    ("vta",          "fire"):                "vta_fire",
    ("vta",          "pathways"):            "vta_pathways",
    ("septum",       "tick"):                "septum_tick",
    ("septum",       "phase_lock"):          "septum_phase_lock",
    ("septum",       "query_bin"):           "septum_query_bin",
    ("raphe",        "fire"):                "raphe_fire",
    ("memory_aging", "tag"):                 "memory_tag",
    ("memory_aging", "capture"):             "memory_capture",
    ("memory_aging", "sweep"):               "memory_aging_sweep",
    ("memory_aging", "tag_get"):             "memory_tag_get",
    ("claustrum",    "record_binding"):      "claustrum_record_binding",
    ("claustrum",    "memory_bindings"):     "claustrum_memory_bindings",
    ("colliculi",    "orient"):              "colliculi_orient",
    ("mammillary",   "log_transit"):         "mammillary_log_transit",
    ("mammillary",   "memory_history"):      "mammillary_memory_history",
    ("mammillary",   "reset_24h"):           "mammillary_reset_24h",
    ("olfactory",    "imprint"):             "olfactory_imprint",
    ("olfactory",    "recall"):              "olfactory_recall",
    ("connectome",   "node_get"):            "connectome_node_get",
    ("connectome",   "neighbors"):           "connectome_neighbors",
    # existing
    ("bg",           "td_emit"):             "bg_td_emit",
    ("bg",           "hold_trigger"):        "bg_hold_trigger",
    ("bg",           "hold_release"):        "bg_hold_release",
    ("bg",           "sweep_traces"):        "bg_sweep_traces",
    ("cerebellum",   "predict"):             "cerebellum_predict",
    ("cerebellum",   "observe"):             "cerebellum_observe",
    ("thalamus",     "salience"):            "thalamus_salience",
    ("thalamus",     "burst"):               "thalamus_burst",
    ("amygdala",     "tag"):                 "amygdala_tag",
    ("amygdala",     "query_valence"):       "amygdala_query_valence",
    ("amygdala",     "extinguish"):          "amygdala_extinguish",
    ("acc",          "evaluate"):            "acc_evaluate",
    ("acc",          "predict"):             "acc_predict",
    ("acc",          "resolve"):             "acc_resolve",
    ("dmn",          "simulate"):            "dmn_simulate",
    ("dmn",          "validate"):            "dmn_validate",
    ("dmn",          "speculative_list"):    "dmn_speculative_list",
    ("drives",       "sample"):              "drive_sample",
    ("drives",       "recommend_mode"):      "drive_recommend_mode",
    ("insula",       "sample"):              "insula_sample",
    ("insula",       "subscribe"):           "insula_subscribe",
    ("insula",       "check_triggers"):      "insula_check_triggers",
    ("pfc",          "slot_set"):            "pfc_slot_set",
    ("pfc",          "slot_get"):            "pfc_slot_get",
    ("hippocampus",  "dg_separate"):         "hippocampus_dg_separate",
    ("hippocampus",  "dg_check"):            "hippocampus_dg_check",
    ("hippocampus",  "ca3_complete"):        "hippocampus_ca3_complete",
    ("entorhinal",   "activate"):            "entorhinal_activate",
    ("entorhinal",   "lookup"):              "entorhinal_lookup",
}

_REGISTER_ROUTE: dict[tuple[str, str], str] = {
    ("lc",         "trigger"):  "lc_register_trigger",
    ("nb",         "target"):   "nb_register_target",
    ("aras",       "trigger"):  "aras_register_trigger",
    ("habenula",   "trigger"):  "habenula_register_trigger",
    ("claustrum",  "modality"): "claustrum_register_modality",
    ("colliculi",  "pattern"):  "colliculi_register_pattern",
    ("connectome", "node"):     "connectome_register_node",
    ("connectome", "edge"):     "connectome_register_edge",
    ("cerebellum", "module"):   "cerebellum_module_register",
    ("bg",         "action"):   "bg_action_register",
    ("drives",     "drive"):    "drive_register",
    ("thalamus",   "relay"):    "thalamus_relay_create",
}

_HISTORY_ROUTE: dict[str, str] = {
    "lc":           "lc_signal_history",
    "nb":           "nb_signal_history",
    "aras":         "aras_history",
    "habenula":     "habenula_history",
    "ca1":          "ca1_subiculum_history",
    "workspace_bw": "workspace_bandwidth_epochs_history",
    "sleep":        "sleep_history",
    "vta":          "vta_history",
    "raphe":        "raphe_history",
    "colliculi":    "colliculi_history",
    "bg":           "bg_shadow_stats",
    "thalamus":     "thalamus_shadow_stats",
}

_CONFIGURE_ROUTE: dict[tuple[str, str], str] = {
    ("lc",           "set_mode"):       "lc_set_mode",
    ("workspace_bw", "set"):            "workspace_bandwidth_set",
    ("vta",          "set_tonic"):      "vta_set_tonic",
    ("septum",       "set_frequency"):  "septum_set_frequency",
    ("raphe",        "set_state"):      "raphe_set_state",
    ("memory_aging", "set"):            "memory_aging_set",
    ("claustrum",    "set"):            "claustrum_set",
    ("olfactory",    "set"):            "olfactory_set",
    ("bg",           "modulator_set"):  "bg_modulator_set",
    ("bg",           "weights_show"):   "bg_weights_show",
    ("bg",           "holds_active"):   "bg_holds_active",
    ("thalamus",     "gate_set"):       "thalamus_gate_set",
    ("thalamus",     "mode_set"):       "thalamus_mode_set",
}

# Topic routers — action-discriminated, not subsystem-keyed
_TOPIC_ROUTES: dict[str, dict[str, str]] = {
    "belief": {
        "collapse":       "belief_collapse",
        "conflicts":      "belief_conflicts",
        "conflicts_scan": "belief_conflicts_scan",
        "consensus":      "belief_consensus",
        "diff":           "belief_diff",
        "get":            "belief_get",
        "merge":          "belief_merge",
        "propagate":      "belief_propagate",
        "seed":           "belief_seed",
        "set":            "belief_set",
        "collapse_log":   "collapse_log",
        "collapse_stats": "collapse_stats",
    },
    "tom": {
        "belief_invalidate":  "tom_belief_invalidate",
        "belief_set":         "tom_belief_set",
        "conflicts_list":     "tom_conflicts_list",
        "conflicts_resolve":  "tom_conflicts_resolve",
        "gap_scan":           "tom_gap_scan",
        "inject":             "tom_inject",
        "perspective_get":    "tom_perspective_get",
        "perspective_set":    "tom_perspective_set",
        "status":             "tom_status",
        "update":             "tom_update",
    },
    "trust": {
        "audit":                "trust_audit",
        "calibrate":            "trust_calibrate",
        "decay":                "trust_decay",
        "process_meb":          "trust_process_meb",
        "show":                 "trust_show",
        "update_contradiction": "trust_update_contradiction",
    },
    "reflexion": {
        "failure_recurrence": "reflexion_failure_recurrence",
        "list":               "reflexion_list",
        "query":              "reflexion_query",
        "retire":             "reflexion_retire",
        "success":            "reflexion_success",
        "write":              "reflexion_write",
    },
    "gaps": {
        "list":    "gaps_list",
        "refresh": "gaps_refresh",
        "resolve": "gaps_resolve",
        "scan":    "gaps_scan",
    },
    "federated": {
        "entity_search": "federated_entity_search",
        "memory_search": "federated_memory_search",
        "search":        "federated_search",
        "stats":         "federated_stats",
    },
    "world": {
        "agent":         "world_agent",
        "predict":       "world_predict",
        "project":       "world_project",
        "resolve":       "world_resolve",
        "status":        "world_status",
        "rebuild_caps":  "world_rebuild_caps",
    },
    "workspace": {
        "ack":       "workspace_ack",
        "broadcast": "workspace_broadcast",
        "history":   "workspace_history",
        "ingest":    "workspace_ingest",
        "phi":       "workspace_phi",
        "status":    "workspace_status",
    },
    "temporal": {
        "auto_detect": "temporal_auto_detect",
        "causes":      "temporal_causes",
        "chain":       "temporal_chain",
        "context":     "temporal_context",
        "effects":     "temporal_effects",
        "map":         "temporal_map",
    },
    "consolidation": {
        "events":   "consolidation_events",
        "run":      "consolidation_run",
        "schedule": "consolidation_schedule",
        "stats":    "consolidation_stats",
    },
    "expertise": {
        "build":  "expertise_build",
        "list":   "expertise_list",
        "show":   "expertise_show",
        "update": "expertise_update",
    },
    "neuro": {
        "detect":  "neuro_detect",
        "history": "neuro_history",
        "set":     "neuro_set",
        "signal":  "neuro_signal",
        "status":  "neuro_status",
        "state":   "neurostate",
    },
    "meb": {
        "prune": "meb_prune",
        "stats": "meb_stats",
        "tail":  "meb_tail",
    },
    "quarantine": {
        "list":   "quarantine_list",
        "purge":  "quarantine_purge",
        "review": "quarantine_review",
    },
    "epoch": {
        "create": "epoch_create",
        "detect": "epoch_detect",
        "list":   "epoch_list",
    },
    "usage": {
        "check":   "usage_check",
        "fleet":   "usage_fleet",
        "log":     "usage_log",
        "summary": "usage_summary",
    },
    "schedule": {
        "run":    "schedule_run",
        "set":    "schedule_set",
        "status": "schedule_status",
    },
    "task": {
        "add":    "task_add",
        "list":   "task_list",
        "update": "task_update",
    },
    "entity_admin": {
        "add_alias":            "entity_add_alias",
        "alias":                "entity_alias",
        "aliases":              "entity_aliases",
        "compile":              "entity_compile",
        "cross_agent_view":     "entity_cross_agent_view",
        "duplicates_scan":      "entity_duplicates_scan",
        "merge":                "entity_merge",
        "reconcile_report":     "entity_reconcile_report",
        "tier":                 "entity_tier",
    },
    "memory_admin": {
        "calibration":         "memory_calibration",
        "attention_snapshot":  "attention_snapshot",
        "replay_boost":        "replay_boost",
        "replay_queue":        "replay_queue",
        "hot":                 "hot_memories",
        "cold":                "cold_memories",
        "promote":             "memory_promote",
        "tier_stats":          "tier_stats",
        "trust_propagate":     "memory_trust_propagate",
        "utility_rate":        "memory_utility_rate",
        "suggest_category":    "memory_suggest_category",
        "pii":                 "memory_pii",
        "pii_scan":            "memory_pii_scan",
    },
    "agent_admin": {
        "activity":  "agent_activity",
        "list":      "agent_list",
        "model":     "agent_model",
        "ping":      "agent_ping",
    },
    "handoff_admin": {
        "consume": "handoff_consume",
        "expire":  "handoff_expire",
        "pin":     "handoff_pin",
    },
    "trigger_admin": {
        "delete": "trigger_delete",
        "list":   "trigger_list",
        "update": "trigger_update",
    },
    "procedure_admin": {
        "backfill": "procedure_backfill",
        "stats":    "procedure_stats",
        "update":   "procedure_update",
        "feedback": "procedure_feedback",
    },
    "policy": {
        "add":      "policy_add",
        "feedback": "policy_feedback",
        "list":     "policy_list",
        "match":    "policy_match",
    },
    "knowledge": {
        "index":   "knowledge_index",
        "report":  "knowledge_report",
        "dreams":  "dreams",
        "distill": "distill",
    },
    "context": {
        "add":    "context_add",
        "search": "context_search",
    },
    "lifecycle": {
        "summary": "lifecycle_summary",
        "decay_report": "decay_report",
        "outcome_annotate": "outcome_annotate",
        "outcome_report":   "outcome_report",
        "outcome_report_annotate": "access_log_annotate",
    },
}

# Subsystem metadata used by subsystem_list()
_SUBSYSTEM_META: dict[str, dict[str, Any]] = {
    "lc":           {"layer": "neuromod_broadcast", "summary": "Locus Coeruleus — fires on +surprise, broadcasts NE"},
    "nb":           {"layer": "neuromod_broadcast", "summary": "Nucleus Basalis — fires on attention shifts, broadcasts ACh"},
    "aras":         {"layer": "global_gate",        "summary": "ARAS — global arousal / sleep-wake state"},
    "habenula":     {"layer": "neuromod_broadcast", "summary": "Lateral habenula — anti-reward / negative-PE"},
    "vta":          {"layer": "neuromod_source",    "summary": "VTA/SNc — dopamine source nucleus"},
    "raphe":        {"layer": "neuromod_source",    "summary": "Raphe nuclei — serotonin source (DRN + MRN)"},
    "septum":       {"layer": "pacemaker",          "summary": "Medial septum — 4-8 Hz theta pacemaker"},
    "ca1":          {"layer": "hippocampus",        "summary": "CA1 + Subiculum — match/mismatch + cortical bridge"},
    "mammillary":   {"layer": "hippocampus",        "summary": "Mammillary + Papez — episodic memory transit log"},
    "sleep":        {"layer": "global_gate",        "summary": "Sleep architecture — 5-stage state machine"},
    "memory_aging": {"layer": "memory_lifecycle",   "summary": "Synaptic tagging-and-capture"},
    "workspace_bw": {"layer": "capacity",           "summary": "Workspace bandwidth — top-K-per-epoch limit"},
    "connectome":   {"layer": "meta_graph",         "summary": "Inter-subsystem communication graph"},
    "claustrum":    {"layer": "binding",            "summary": "Cross-modal retrieval-modality binding"},
    "colliculi":    {"layer": "orienting",          "summary": "SC + IC — orienting reflex"},
    "olfactory":    {"layer": "orienting",          "summary": "Olfactory — direct binding (bypasses thalamus)"},
    "bg":           {"layer": "action_selection",   "summary": "Basal ganglia — 5-loop action selection + Go/NoGo"},
    "cerebellum":   {"layer": "forward_model",      "summary": "Cerebellum — predict/observe per cortical partner"},
    "thalamus":     {"layer": "routing_gate",       "summary": "Thalamus — typed routing + salience + shadow gate"},
    "amygdala":     {"layer": "valence",            "summary": "Amygdala — rapid valence/threat tagging"},
    "hippocampus":  {"layer": "hippocampus",        "summary": "Hippocampal subfields — DG/CA3 audit"},
    "acc":          {"layer": "control",            "summary": "Anterior cingulate — conflict / surprise / EVC monitor"},
    "dmn":          {"layer": "offline",            "summary": "Default mode network — offline counterfactual simulation"},
    "drives":       {"layer": "homeostatic",        "summary": "Hypothalamic drives — homeostatic"},
    "insula":       {"layer": "interoception",      "summary": "Insula — self-state vector + subscribers"},
    "pfc":          {"layer": "executive",          "summary": "PFC named slots — dlPFC/vmPFC/OFC/frontopolar"},
    "entorhinal":   {"layer": "indexing",           "summary": "Entorhinal grid — 48-cell conceptual index"},
}


# ---------------------------------------------------------------- dispatcher tools


def tool_subsystem_list(layer: str | None = None, **_kw: Any) -> dict[str, Any]:
    """List all subsystems and their layer + summary. Filter by layer if given."""
    items = [
        {"name": name, **meta}
        for name, meta in sorted(_SUBSYSTEM_META.items())
        if layer is None or meta.get("layer") == layer
    ]
    return {"ok": True, "subsystems": items, "count": len(items)}


def tool_subsystem_list_actions(name: str, **_kw: Any) -> dict[str, Any]:
    """List all valid actions, register kinds, and configure fields for a subsystem."""
    actions = sorted({a for (s, a) in _EMIT_ROUTE.keys() if s == name})
    kinds = sorted({k for (s, k) in _REGISTER_ROUTE.keys() if s == name})
    fields = sorted({f for (s, f) in _CONFIGURE_ROUTE.keys() if s == name})
    has_status = name in _STATUS_ROUTE
    has_history = name in _HISTORY_ROUTE
    if not (actions or kinds or fields or has_status or has_history):
        return {"error": f"unknown subsystem {name!r}. Call subsystem_list."}
    return {
        "ok": True, "name": name,
        "supports_status": has_status,
        "supports_history": has_history,
        "emit_actions": actions,
        "register_kinds": kinds,
        "configure_fields": fields,
    }


def tool_subsystem_status(name: str, agent_id: str | None = None, **_kw: Any) -> dict[str, Any]:
    """Return state + recent activity for a named subsystem."""
    target = _STATUS_ROUTE.get(name)
    if target is None:
        return {"error": f"unknown subsystem {name!r}. Call subsystem_list."}
    return _call_by_name(target, {"agent_id": agent_id} if agent_id else {})


def tool_subsystem_emit(name: str, action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    target = _EMIT_ROUTE.get((name, action))
    if target is None:
        return {"error": f"unknown (subsystem={name!r}, action={action!r}). Call subsystem_list_actions."}
    return _call_by_name(target, payload)


def tool_subsystem_register(name: str, kind: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    target = _REGISTER_ROUTE.get((name, kind))
    if target is None:
        return {"error": f"unknown (subsystem={name!r}, kind={kind!r}). Call subsystem_list_actions."}
    return _call_by_name(target, payload)


def tool_subsystem_history(name: str, filters: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    target = _HISTORY_ROUTE.get(name)
    if target is None:
        return {"error": f"unknown subsystem {name!r} for history. Call subsystem_list_actions."}
    return _call_by_name(target, filters)


def tool_subsystem_configure(name: str, field: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    target = _CONFIGURE_ROUTE.get((name, field))
    if target is None:
        return {"error": f"unknown (subsystem={name!r}, field={field!r}). Call subsystem_list_actions."}
    return _call_by_name(target, payload)


def _topic(topic: str, action: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    routes = _TOPIC_ROUTES.get(topic)
    if routes is None:
        return {"error": f"unknown topic {topic!r}"}
    target = routes.get(action)
    if target is None:
        return {"error": f"unknown action {action!r} for topic {topic!r}. Valid: {sorted(routes.keys())}"}
    return _call_by_name(target, payload)


def tool_belief(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("belief", action, payload)


def tool_tom(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("tom", action, payload)


def tool_trust(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("trust", action, payload)


def tool_reflexion(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("reflexion", action, payload)


def tool_gaps(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("gaps", action, payload)


def tool_federated(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("federated", action, payload)


def tool_world(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("world", action, payload)


def tool_workspace(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("workspace", action, payload)


def tool_temporal(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("temporal", action, payload)


def tool_consolidation(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("consolidation", action, payload)


def tool_expertise(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("expertise", action, payload)


def tool_neuro(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("neuro", action, payload)


def tool_meb(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("meb", action, payload)


def tool_quarantine(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("quarantine", action, payload)


def tool_epoch(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("epoch", action, payload)


def tool_usage(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("usage", action, payload)


def tool_schedule(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("schedule", action, payload)


def tool_task(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("task", action, payload)


def tool_entity_admin(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("entity_admin", action, payload)


def tool_memory_admin(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("memory_admin", action, payload)


def tool_agent_admin(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("agent_admin", action, payload)


def tool_handoff_admin(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("handoff_admin", action, payload)


def tool_trigger_admin(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("trigger_admin", action, payload)


def tool_procedure_admin(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("procedure_admin", action, payload)


def tool_policy(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("policy", action, payload)


def tool_knowledge(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("knowledge", action, payload)


def tool_context(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("context", action, payload)


def tool_lifecycle(action: str, payload: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
    return _topic("lifecycle", action, payload)


# ---------------------------------------------------------------- MCP tool list

_OBJ = {"type": "object", "additionalProperties": True}

TOOLS: list[Tool] = [
    Tool(
        name="subsystem_list",
        description=(
            "Discoverability: list all brainctl brain-region / nucleus / meta subsystems "
            "with their layer + 1-line summary. Optional filter by `layer`. Call this "
            "first to learn what subsystems exist; then `subsystem_list_actions(name)` "
            "to learn what actions each supports."
        ),
        inputSchema={"type": "object", "properties": {"layer": {"type": "string"}}},
    ),
    Tool(
        name="subsystem_list_actions",
        description=(
            "List the actions/kinds/fields valid for a named subsystem. Call before "
            "subsystem_emit / subsystem_register / subsystem_configure."
        ),
        inputSchema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    ),
    Tool(
        name="subsystem_status",
        description=(
            "Current state + recent activity for a named subsystem. Replaces 27 per-subsystem "
            "*_status tools (lc_status, nb_status, aras_status, habenula_status, bg_status, "
            "cerebellum_status, thalamus_status, amygdala_status, acc_status, dmn_*_status, "
            "drive_status, insula_state, pfc_status, entorhinal_status, etc.). "
            "Discover names via subsystem_list."
        ),
        inputSchema={
            "type": "object",
            "properties": {"name": {"type": "string"}, "agent_id": {"type": "string"}},
            "required": ["name"],
        },
    ),
    Tool(
        name="subsystem_emit",
        description=(
            "Fire / record an event on a subsystem (fire, transition, tag, predict, observe, "
            "advance, etc.). payload is forwarded as kwargs. Examples: "
            "(name='lc', action='fire', payload={trigger_name:'cerebellum_high_pe', surprise_magnitude:0.7}); "
            "(name='aras', action='transition', payload={to_mode:'awake_focused'}); "
            "(name='bg', action='td_emit', payload={...}). Discover valid actions per "
            "subsystem via subsystem_list_actions."
        ),
        inputSchema={
            "type": "object",
            "properties": {"name": {"type": "string"}, "action": {"type": "string"}, "payload": _OBJ},
            "required": ["name", "action"],
        },
    ),
    Tool(
        name="subsystem_register",
        description=(
            "Idempotent UPSERT into a subsystem's catalog (triggers, targets, patterns, "
            "modules, edges, etc.). Example: "
            "(name='lc', kind='trigger', payload={name:'x', source_table:'...', default_ne_delta:0.1}); "
            "(name='connectome', kind='edge', payload={source:'a', target:'b', edge_type:'writes_to'}). "
            "Discover valid kinds via subsystem_list_actions."
        ),
        inputSchema={
            "type": "object",
            "properties": {"name": {"type": "string"}, "kind": {"type": "string"}, "payload": _OBJ},
            "required": ["name", "kind"],
        },
    ),
    Tool(
        name="subsystem_history",
        description=(
            "Paginated history of events / firings / transitions for a subsystem. "
            "`filters` is forwarded as kwargs (typically supports limit, since, agent_id, "
            "plus subsystem-specific filters)."
        ),
        inputSchema={
            "type": "object",
            "properties": {"name": {"type": "string"}, "filters": _OBJ},
            "required": ["name"],
        },
    ),
    Tool(
        name="subsystem_configure",
        description=(
            "Update subsystem state / mode / config. `field` selects the configure action. "
            "Examples: (name='lc', field='set_mode', payload={mode:'tonic_high'}); "
            "(name='workspace_bw', field='set', payload={enforcement_mode:'enforce'})."
        ),
        inputSchema={
            "type": "object",
            "properties": {"name": {"type": "string"}, "field": {"type": "string"}, "payload": _OBJ},
            "required": ["name", "field"],
        },
    ),
    Tool(
        name="belief",
        description="Belief-system operations. action ∈ {collapse, conflicts, conflicts_scan, consensus, diff, get, merge, propagate, seed, set, collapse_log, collapse_stats}. Replaces belief_* + belief_merge + collapse_* tools.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="tom",
        description="Theory-of-Mind operations. action ∈ {belief_invalidate, belief_set, conflicts_list, conflicts_resolve, gap_scan, inject, perspective_get, perspective_set, status, update}. Replaces the 10 tom_* tools.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="trust",
        description="Trust calibration / audit. action ∈ {audit, calibrate, decay, process_meb, show, update_contradiction}. Replaces the 6 trust_* tools.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="reflexion",
        description="Reflexion / failure-driven self-correction. action ∈ {failure_recurrence, list, query, retire, success, write}. Replaces 6 reflexion_* tools.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="gaps",
        description="Knowledge-gap scanner. action ∈ {list, refresh, resolve, scan}. Replaces 4 gaps_* tools.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="federated",
        description="Federated cross-tenant search. action ∈ {entity_search, memory_search, search, stats}. Replaces 4 federated_* tools.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="world",
        description="World model. action ∈ {agent, predict, project, resolve, status, rebuild_caps}. Replaces 6 world_* tools.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="workspace",
        description="Global Neuronal Workspace (broadcasts table). action ∈ {ack, broadcast, history, ingest, phi, status}. Replaces 6 workspace_* tools. NOTE: workspace_bandwidth (new tonight) is a separate subsystem accessed via subsystem_* dispatchers.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="temporal",
        description="Temporal reasoning. action ∈ {auto_detect, causes, chain, context, effects, map}. Replaces 6 temporal_* tools.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="consolidation",
        description="Memory consolidation. action ∈ {events, run, schedule, stats}. Replaces 4 consolidation_* tools.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="expertise",
        description="Expertise profiles. action ∈ {build, list, show, update}. Replaces 4 expertise_* tools.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="neuro",
        description="Neuromodulation state. action ∈ {detect, history, set, signal, status, state}. Replaces 5 neuro_* tools + neurostate.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="meb",
        description="MEB (memory event buffer). action ∈ {prune, stats, tail}. Replaces 3 meb_* tools.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="quarantine",
        description="Memory immunity / poisoned-memory handling. action ∈ {list, purge, review}. Replaces 3 quarantine_* tools.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="epoch",
        description="Epoch management. action ∈ {create, detect, list}. Replaces 3 epoch_* tools.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="usage",
        description="Usage tracking. action ∈ {check, fleet, log, summary}. Replaces 4 usage_* tools.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="schedule",
        description="Schedule management. action ∈ {run, set, status}. Replaces 3 schedule_* tools.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="task",
        description="Task tracking. action ∈ {add, list, update}. Replaces 3 task_* tools.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="policy",
        description="Policy rules. action ∈ {add, feedback, list, match}. Replaces 4 policy_* tools.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="knowledge",
        description="Knowledge index + distillation. action ∈ {index, report, dreams, distill}. Replaces knowledge_*/dreams/distill.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="context",
        description="Context bag. action ∈ {add, search}. Replaces context_* tools.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="lifecycle",
        description="Memory lifecycle. action ∈ {summary, decay_report, outcome_annotate, outcome_report, outcome_report_annotate}. Replaces lifecycle_summary, decay_report, outcome_*, access_log_annotate.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="entity_admin",
        description="Entity catalog admin ops. action ∈ {add_alias, alias, aliases, compile, cross_agent_view, duplicates_scan, merge, reconcile_report, tier}. Primary entity tools (entity_create/get/search/observe/relate) stay direct.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="memory_admin",
        description="Memory admin ops. action ∈ {calibration, attention_snapshot, replay_boost, replay_queue, hot, cold, promote, tier_stats, trust_propagate, utility_rate, suggest_category, pii, pii_scan}. Primary memory tools (memory_add, memory_search) stay direct.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="agent_admin",
        description="Agent admin ops. action ∈ {activity, list, model, ping}. Primary agent tools (agent_orient, brainctl_wrapup, agent_register) stay direct.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="handoff_admin",
        description="Handoff admin ops. action ∈ {consume, expire, pin}. Primary handoff tools (handoff_add, handoff_latest) stay direct.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="trigger_admin",
        description="Trigger admin ops. action ∈ {delete, list, update}. Primary trigger tools (trigger_create, trigger_check) stay direct.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
    Tool(
        name="procedure_admin",
        description="Procedure admin ops. action ∈ {backfill, stats, update, feedback}. Primary procedure tools (procedure_add, procedure_get, procedure_search) stay direct.",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}, "payload": _OBJ}, "required": ["action"]},
    ),
]


_CONSOLIDATED = {
    "subsystem_list":         tool_subsystem_list,
    "subsystem_list_actions": tool_subsystem_list_actions,
    "subsystem_status":       tool_subsystem_status,
    "subsystem_emit":         tool_subsystem_emit,
    "subsystem_register":     tool_subsystem_register,
    "subsystem_history":      tool_subsystem_history,
    "subsystem_configure":    tool_subsystem_configure,
    "belief":                 tool_belief,
    "tom":                    tool_tom,
    "trust":                  tool_trust,
    "reflexion":              tool_reflexion,
    "gaps":                   tool_gaps,
    "federated":              tool_federated,
    "world":                  tool_world,
    "workspace":              tool_workspace,
    "temporal":               tool_temporal,
    "consolidation":          tool_consolidation,
    "expertise":              tool_expertise,
    "neuro":                  tool_neuro,
    "meb":                    tool_meb,
    "quarantine":             tool_quarantine,
    "epoch":                  tool_epoch,
    "usage":                  tool_usage,
    "schedule":               tool_schedule,
    "task":                   tool_task,
    "policy":                 tool_policy,
    "knowledge":              tool_knowledge,
    "context":                tool_context,
    "lifecycle":              tool_lifecycle,
    "entity_admin":           tool_entity_admin,
    "memory_admin":           tool_memory_admin,
    "agent_admin":            tool_agent_admin,
    "handoff_admin":          tool_handoff_admin,
    "trigger_admin":          tool_trigger_admin,
    "procedure_admin":        tool_procedure_admin,
}

DISPATCH: dict[str, Any] = {
    name: (lambda _func=func, **kw: _func(**kw))
    for name, func in _CONSOLIDATED.items()
}


# ---------------------------------------------------------------- deprecation surface

# v1 tool names that this consolidation subsumes. mcp_server.py filters its
# public TOOLS list against this set so deprecated tools are not visible in
# `tools/list` — but their underlying DISPATCH entries remain intact for
# internal use and trivial rollback.

DEPRECATED_TOOL_NAMES: frozenset[str] = frozenset({
    # Per-subsystem *_status that subsystem_status replaces
    "lc_status", "nb_status", "aras_status", "habenula_status", "ca1_status",
    "workspace_bandwidth_status", "connectome_status", "sleep_status",
    "vta_status", "septum_status", "raphe_status", "memory_aging_status",
    "claustrum_status", "colliculi_status", "mammillary_status", "olfactory_status",
    "bg_status", "cerebellum_status", "thalamus_status", "amygdala_status",
    "hippocampus_subfields_status", "acc_status", "dmn_schedule_status",
    "drive_status", "insula_state", "pfc_status", "entorhinal_status",
    # *_fire / emit-style
    "lc_fire", "nb_fire", "nb_attend_sector", "aras_transition", "aras_drive",
    "habenula_fire", "habenula_reset", "ca1_compare", "subiculum_output",
    "workspace_bandwidth_admit", "sleep_transition", "sleep_advance",
    "sleep_operation_permitted", "vta_fire", "septum_tick", "septum_phase_lock",
    "septum_query_bin", "raphe_fire", "memory_tag", "memory_capture",
    "memory_aging_sweep", "memory_tag_get", "claustrum_record_binding",
    "claustrum_memory_bindings", "colliculi_orient", "mammillary_log_transit",
    "mammillary_memory_history", "mammillary_reset_24h", "olfactory_imprint",
    "olfactory_recall", "connectome_node_get", "connectome_neighbors",
    "bg_td_emit", "bg_hold_trigger", "bg_hold_release", "bg_sweep_traces",
    "cerebellum_predict", "cerebellum_observe", "thalamus_salience",
    "thalamus_burst", "amygdala_tag", "amygdala_query_valence",
    "amygdala_extinguish", "acc_evaluate", "acc_predict", "acc_resolve",
    "dmn_simulate", "dmn_validate", "dmn_speculative_list", "drive_sample",
    "drive_recommend_mode", "insula_sample", "insula_subscribe",
    "insula_check_triggers", "pfc_slot_set", "pfc_slot_get",
    "hippocampus_dg_separate", "hippocampus_dg_check", "hippocampus_ca3_complete",
    "entorhinal_activate", "entorhinal_lookup",
    # *_register / catalog UPSERTs
    "lc_register_trigger", "nb_register_target", "aras_register_trigger",
    "habenula_register_trigger", "claustrum_register_modality",
    "colliculi_register_pattern", "connectome_register_node",
    "connectome_register_edge", "cerebellum_module_register",
    "bg_action_register", "drive_register", "thalamus_relay_create",
    # *_history
    "lc_signal_history", "nb_signal_history", "aras_history", "habenula_history",
    "ca1_subiculum_history", "workspace_bandwidth_epochs_history",
    "sleep_history", "vta_history", "raphe_history", "colliculi_history",
    "bg_shadow_stats", "thalamus_shadow_stats",
    # *_set / configure
    "lc_set_mode", "workspace_bandwidth_set", "vta_set_tonic", "vta_pathways",
    "septum_set_frequency", "raphe_set_state", "memory_aging_set",
    "claustrum_set", "olfactory_set", "bg_modulator_set", "bg_weights_show",
    "bg_holds_active", "thalamus_gate_set", "thalamus_mode_set",
    # Belief / ToM / trust / reflexion / gaps / federated
    "belief_collapse", "belief_conflicts", "belief_conflicts_scan",
    "belief_consensus", "belief_diff", "belief_get", "belief_merge",
    "belief_propagate", "belief_seed", "belief_set",
    "collapse_log", "collapse_stats",
    "tom_belief_invalidate", "tom_belief_set", "tom_conflicts_list",
    "tom_conflicts_resolve", "tom_gap_scan", "tom_inject",
    "tom_perspective_get", "tom_perspective_set", "tom_status", "tom_update",
    "trust_audit", "trust_calibrate", "trust_decay", "trust_process_meb",
    "trust_show", "trust_update_contradiction",
    "reflexion_failure_recurrence", "reflexion_list", "reflexion_query",
    "reflexion_retire", "reflexion_success", "reflexion_write",
    "gaps_list", "gaps_refresh", "gaps_resolve", "gaps_scan",
    "federated_entity_search", "federated_memory_search",
    "federated_search", "federated_stats",
    # Additional consolidated clusters
    "world_agent", "world_predict", "world_project", "world_resolve",
    "world_status", "world_rebuild_caps",
    "workspace_ack", "workspace_broadcast", "workspace_history",
    "workspace_ingest", "workspace_phi", "workspace_status",
    "temporal_auto_detect", "temporal_causes", "temporal_chain",
    "temporal_context", "temporal_effects", "temporal_map",
    "consolidation_events", "consolidation_run", "consolidation_schedule",
    "consolidation_stats",
    "expertise_build", "expertise_list", "expertise_show", "expertise_update",
    "neuro_detect", "neuro_history", "neuro_set", "neuro_signal",
    "neuro_status", "neurostate",
    "meb_prune", "meb_stats", "meb_tail",
    "quarantine_list", "quarantine_purge", "quarantine_review",
    "epoch_create", "epoch_detect", "epoch_list",
    "usage_check", "usage_fleet", "usage_log", "usage_summary",
    "schedule_run", "schedule_set", "schedule_status",
    "task_add", "task_list", "task_update",
    "policy_add", "policy_feedback", "policy_list", "policy_match",
    "knowledge_index", "knowledge_report", "dreams", "distill",
    "context_add", "context_search",
    "lifecycle_summary", "decay_report",
    "outcome_annotate", "outcome_report", "access_log_annotate",
    "entity_add_alias", "entity_alias", "entity_aliases", "entity_compile",
    "entity_cross_agent_view", "entity_duplicates_scan", "entity_merge",
    "entity_reconcile_report", "entity_tier",
    "memory_calibration", "attention_snapshot", "replay_boost", "replay_queue",
    "hot_memories", "cold_memories", "memory_promote", "tier_stats",
    "memory_trust_propagate", "memory_utility_rate", "memory_suggest_category",
    "memory_pii", "memory_pii_scan",
    "agent_activity", "agent_list", "agent_model", "agent_ping",
    "handoff_consume", "handoff_expire", "handoff_pin",
    "trigger_delete", "trigger_list", "trigger_update",
    "procedure_backfill", "procedure_stats", "procedure_update",
    "procedure_feedback",
    # Not part of the v1->v2 subsystem consolidation above -- a straight
    # rename (2026-08-10) for naming clarity. agent_wrap_up's description
    # read as "the" session-end action; it only ever wrote brainctl's own
    # two continuity records (session_end event + handoff packet). Renamed
    # to brainctl_wrapup so the name itself signals brainctl-scope. Same
    # underlying function, still fully dispatchable under the old name too.
    "agent_wrap_up",
})

# Deprecated names that intentionally have no v1->v2 consolidation route:
# they're not a subsystem being folded into a broader tool, they're a pure
# rename kept dispatchable under the old name for backward compatibility.
# test_every_deprecated_v1_tool_has_a_v2_route treats anything in
# DEPRECATED_TOOL_NAMES with no route as a bug unless it's listed here.
_ACCEPTED_HIDDEN_ORPHANS: frozenset[str] = frozenset({
    # agent_wrap_up -> brainctl_wrapup (2026-08-10): same underlying
    # function (tool_agent_wrap_up in mcp_server.py), reachable directly
    # under both names via DISPATCH, not through this module's route
    # tables -- there is no separate v2 tool for it to route to.
    "agent_wrap_up",
})


def register_tools() -> tuple[list[Tool], dict[str, Any]]:
    return TOOLS, DISPATCH
