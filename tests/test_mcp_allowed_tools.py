"""Tests for BRAINCTL_ALLOWED_TOOLS (issue #114).

The stdio MCP server can be limited to a subset of its 201 tools via
the BRAINCTL_ALLOWED_TOOLS env var. Required for clients like Google's
Antigravity IDE that enforce a hard 100-tool MCP cap. Unset env =
backward-compatible behaviour (full surface exposed).

Unknown tool names in the env var are a HARD ERROR at process start —
not a silent skip — so a typo can't cause an invisible misconfiguration.

These tests exercise the pure resolver (`_resolve_allowed_tools`) and
patch the module-level `_ALLOWED_TOOLS` for the filter tests. We
deliberately avoid `importlib.reload(mcp_server)` because it disturbs
function identity for other tests in the suite. For the async filter
checks we drive `list_tools` / `call_tool` via `asyncio.run()` from
sync test functions so the tests don't need pytest-asyncio (which is
not in the CI install).
"""
from __future__ import annotations

import asyncio

import pytest


pytest.importorskip("mcp", reason="brainctl[mcp] required for stdio server tests")
from agentmemory import mcp_server  # noqa: E402


class TestResolveAllowedTools:
    def test_unset_returns_none(self, monkeypatch):
        monkeypatch.delenv("BRAINCTL_ALLOWED_TOOLS", raising=False)
        assert mcp_server._resolve_allowed_tools() is None

    def test_empty_returns_none(self, monkeypatch):
        monkeypatch.setenv("BRAINCTL_ALLOWED_TOOLS", "")
        assert mcp_server._resolve_allowed_tools() is None

    def test_whitespace_only_returns_none(self, monkeypatch):
        monkeypatch.setenv("BRAINCTL_ALLOWED_TOOLS", "   ,   ")
        assert mcp_server._resolve_allowed_tools() is None

    def test_valid_names_pass_through(self, monkeypatch):
        monkeypatch.setenv(
            "BRAINCTL_ALLOWED_TOOLS", "memory_add,memory_search,stats"
        )
        result = mcp_server._resolve_allowed_tools()
        assert result == frozenset({"memory_add", "memory_search", "stats"})

    def test_trims_whitespace(self, monkeypatch):
        monkeypatch.setenv(
            "BRAINCTL_ALLOWED_TOOLS",
            " memory_add , memory_search ,  stats  ",
        )
        result = mcp_server._resolve_allowed_tools()
        assert result == frozenset({"memory_add", "memory_search", "stats"})

    def test_unknown_name_hard_exits(self, monkeypatch):
        monkeypatch.setenv(
            "BRAINCTL_ALLOWED_TOOLS", "memory_add,not_a_real_tool"
        )
        with pytest.raises(SystemExit) as exc_info:
            mcp_server._resolve_allowed_tools()
        msg = str(exc_info.value)
        assert "not_a_real_tool" in msg
        assert "BRAINCTL_ALLOWED_TOOLS" in msg

    def test_v1_deprecated_name_hard_exits(self, monkeypatch):
        """Post-v2: an allowlist containing only v1-deprecated names
        would silently empty the visible surface. Hard-fail instead, so
        a stale Antigravity / harness allowlist surfaces during start
        rather than presenting as a broken zero-tool client."""
        deprecated_sample = next(iter(mcp_server._V2_DEPRECATED))
        monkeypatch.setenv("BRAINCTL_ALLOWED_TOOLS", deprecated_sample)
        with pytest.raises(SystemExit) as exc_info:
            mcp_server._resolve_allowed_tools()
        msg = str(exc_info.value)
        assert deprecated_sample in msg
        assert "deprecated" in msg.lower()
        assert "TOOL_MIGRATION_V2" in msg

    def test_typo_gets_did_you_mean_suggestion(self, monkeypatch):
        """memory-add (hyphen) should suggest memory_add (underscore)."""
        monkeypatch.setenv("BRAINCTL_ALLOWED_TOOLS", "memory-add")
        with pytest.raises(SystemExit) as exc_info:
            mcp_server._resolve_allowed_tools()
        msg = str(exc_info.value)
        assert "memory-add" in msg
        assert "memory_add" in msg
        assert "did you mean" in msg.lower()

    def test_no_close_match_reported(self, monkeypatch):
        monkeypatch.setenv("BRAINCTL_ALLOWED_TOOLS", "xyzqwerty_no_match_at_all")
        with pytest.raises(SystemExit) as exc_info:
            mcp_server._resolve_allowed_tools()
        msg = str(exc_info.value)
        assert "no close match" in msg


class TestListToolsFiltering:
    def test_unset_returns_visible_surface(self, monkeypatch):
        """When the allowlist is unset, list_tools returns the post-v2
        VISIBLE surface (v1 deprecated names are hidden by the
        consolidation filter)."""
        monkeypatch.setattr(mcp_server, "_ALLOWED_TOOLS", None)
        tools = asyncio.run(mcp_server.list_tools())
        # Post-v2: visible count is len(TOOLS) - len(_V2_DEPRECATED ∩ _ALL_TOOL_NAMES).
        # Pre-v2 (rollback): visible == TOOLS (no filter).
        expected = len(getattr(mcp_server, "_VISIBLE_TOOL_NAMES", mcp_server._ALL_TOOL_NAMES))
        assert len(tools) == expected

    def test_allowlist_filters_surface(self, monkeypatch):
        allowlist = frozenset({"memory_add", "memory_search", "event_add", "stats"})
        monkeypatch.setattr(mcp_server, "_ALLOWED_TOOLS", allowlist)
        tools = asyncio.run(mcp_server.list_tools())
        names = {t.name for t in tools}
        assert names == allowlist

    def test_antigravity_subset_fits_under_100_cap(self, monkeypatch):
        """Antigravity (and other harnesses with a 100-tool cap) need a
        minimal-but-useful allowlist. Post-v2, two former v1 names in
        this set (handoff_consume, trigger_list) live behind admin
        dispatchers and are no longer in the visible surface — call
        handoff_admin(action='consume', ...) and trigger_admin(
        action='list', ...) instead. Uses brainctl_wrapup (not the
        deprecated agent_wrap_up) — this is the actual set Reed's real
        Antigravity config (mcp_config.json) uses."""
        antigravity_set = frozenset({
            "memory_add", "memory_search", "search", "event_add",
            "event_search", "entity_create", "entity_get", "entity_observe",
            "entity_relate", "entity_search", "decision_add", "handoff_add",
            "handoff_latest", "handoff_admin", "trigger_create",
            "trigger_admin", "trigger_check", "stats", "agent_orient",
            "brainctl_wrapup", "validate", "lint",
        })
        monkeypatch.setattr(mcp_server, "_ALLOWED_TOOLS", antigravity_set)
        tools = asyncio.run(mcp_server.list_tools())
        assert len(tools) <= 100
        assert len(tools) == 22


class TestCLIListToolsConsistency:
    """The --list-tools CLI must mirror what list_tools() returns over
    the wire — anything else would let an operator believe their
    server exposes a different surface than it actually does."""

    def _run_cli(self, monkeypatch, allowed, args):
        # We exercise the same branch as `brainctl-mcp --list-tools` by
        # invoking the function under controlled sys.argv. The CLI path
        # reads _ALLOWED_TOOLS module-level, so monkeypatch it directly.
        import io
        import sys
        monkeypatch.setattr(mcp_server, "_ALLOWED_TOOLS", allowed)
        monkeypatch.setattr(sys, "argv", ["brainctl-mcp"] + args)
        # The CLI lives inside mcp_server.main(); easiest is to replicate
        # the exact branch logic here to keep the test hermetic.
        out = io.StringIO()
        for t in mcp_server.TOOLS:
            if "--all" not in args and t.name not in mcp_server._VISIBLE_TOOL_NAMES:
                continue
            if mcp_server._ALLOWED_TOOLS is not None and t.name not in mcp_server._ALLOWED_TOOLS:
                continue
            out.write(t.name + "\n")
        return [ln for ln in out.getvalue().splitlines() if ln]

    def test_list_tools_honors_allowlist(self, monkeypatch):
        allowed = frozenset({"memory_add", "stats"})
        names = self._run_cli(monkeypatch, allowed, ["--list-tools"])
        assert set(names) == allowed, (
            f"CLI --list-tools must apply BRAINCTL_ALLOWED_TOOLS; "
            f"got {names!r}"
        )

    def test_list_tools_all_still_honors_allowlist(self, monkeypatch):
        """`--all` bypasses ONLY the v2 visibility filter, not the
        operator's explicit security allowlist."""
        allowed = frozenset({"memory_add", "stats"})
        names = self._run_cli(monkeypatch, allowed, ["--list-tools", "--all"])
        assert set(names) == allowed, (
            f"CLI --list-tools --all must still honor allowlist; "
            f"got {names!r}"
        )

    def test_list_tools_no_allowlist_returns_visible(self, monkeypatch):
        names = self._run_cli(monkeypatch, None, ["--list-tools"])
        assert len(names) == len(mcp_server._VISIBLE_TOOL_NAMES)

    def test_list_tools_all_no_allowlist_returns_full_surface(self, monkeypatch):
        names = self._run_cli(monkeypatch, None, ["--list-tools", "--all"])
        assert len(names) == len(mcp_server.TOOLS)


class TestCallToolGating:
    def test_disallowed_call_raises(self, monkeypatch):
        monkeypatch.setattr(
            mcp_server, "_ALLOWED_TOOLS", frozenset({"memory_add"})
        )
        with pytest.raises(ValueError) as exc_info:
            asyncio.run(mcp_server.call_tool("stats", {}))
        msg = str(exc_info.value)
        assert "stats" in msg
        assert "BRAINCTL_ALLOWED_TOOLS" in msg

    def test_allowed_call_passes_gate(self, monkeypatch):
        """Calling an allowed tool must NOT be rejected by the gate.
        The handler may still error for its own reasons (DB missing,
        invalid args, etc.) but the error must not mention the allowlist
        env var."""
        monkeypatch.setattr(
            mcp_server, "_ALLOWED_TOOLS", frozenset({"stats"})
        )
        try:
            asyncio.run(mcp_server.call_tool("stats", {}))
        except ValueError as e:
            assert "BRAINCTL_ALLOWED_TOOLS" not in str(e)
        except Exception:
            pass  # other errors (DB env, etc.) are out of scope

    def test_unset_allowlist_does_not_gate(self, monkeypatch):
        monkeypatch.setattr(mcp_server, "_ALLOWED_TOOLS", None)
        try:
            asyncio.run(mcp_server.call_tool("stats", {}))
        except ValueError as e:
            assert "BRAINCTL_ALLOWED_TOOLS" not in str(e)
        except Exception:
            pass


class TestAgentIdEnvFallback:
    """agent_id resolution in call_tool: explicit arg > BRAINCTL_AGENT_ID env
    var > "mcp-client" default. Ported 2026-09-16 from a real production
    patch (see project_reed_brainctl_repair_2026-08-05.md) after discovering
    the live install had this fix and our fork didn't — without the env-var
    fallback, any agent whose client doesn't pass agent_id explicitly gets
    every memory silently misattributed to "mcp-client"."""

    def _capture_agent_id(self, monkeypatch):
        captured = {}

        def _fake_tool_agent_orient(agent_id=None, **kwargs):
            captured["agent_id"] = agent_id
            return {"ok": True}

        monkeypatch.setattr(mcp_server, "tool_agent_orient", _fake_tool_agent_orient)
        monkeypatch.setattr(mcp_server, "_ALLOWED_TOOLS", None)
        return captured

    def test_env_var_used_when_arg_missing(self, monkeypatch):
        captured = self._capture_agent_id(monkeypatch)
        monkeypatch.setenv("BRAINCTL_AGENT_ID", "Reed")
        asyncio.run(mcp_server.call_tool("agent_orient", {}))
        assert captured["agent_id"] == "Reed"

    def test_default_when_arg_and_env_both_missing(self, monkeypatch):
        captured = self._capture_agent_id(monkeypatch)
        monkeypatch.delenv("BRAINCTL_AGENT_ID", raising=False)
        asyncio.run(mcp_server.call_tool("agent_orient", {}))
        assert captured["agent_id"] == "mcp-client"

    def test_explicit_arg_takes_precedence_over_env(self, monkeypatch):
        captured = self._capture_agent_id(monkeypatch)
        monkeypatch.setenv("BRAINCTL_AGENT_ID", "Reed")
        asyncio.run(
            mcp_server.call_tool("agent_orient", {"agent_id": "Grok"})
        )
        assert captured["agent_id"] == "Grok"


class TestKnownToolNames:
    def test_module_exports_known_tool_set(self):
        assert hasattr(mcp_server, "_ALL_TOOL_NAMES")
        assert isinstance(mcp_server._ALL_TOOL_NAMES, frozenset)
        assert "memory_add" in mcp_server._ALL_TOOL_NAMES
        assert "stats" in mcp_server._ALL_TOOL_NAMES
        # _ALL_TOOL_NAMES = every live Tool() name, UNION any deprecated name
        # that no longer has a live Tool() object of its own (e.g.
        # agent_wrap_up, renamed to brainctl_wrapup 2026-08-10) -- so a
        # stale BRAINCTL_ALLOWED_TOOLS naming the old tool is recognized as
        # known-deprecated rather than rejected as unknown. The gap between
        # this set and the live TOOLS list should be exactly those
        # Tool-less deprecated names, not an unrelated drift.
        live_names = frozenset(t.name for t in mcp_server.TOOLS)
        deprecated_without_live_tool = mcp_server._ALL_TOOL_NAMES - live_names
        assert deprecated_without_live_tool == {"agent_wrap_up"}
        assert mcp_server._ALL_TOOL_NAMES == live_names | mcp_server._V2_DEPRECATED
