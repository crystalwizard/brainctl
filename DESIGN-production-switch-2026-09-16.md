# Design doc: switching production brainctl to our fork

Written 2026-09-17, per Kelly's standing design-first process
(`feedback_design_implementation_process.md`) -- pausing the reactive
fix-and-commit cycle to lay the whole remaining task out calmly before
touching anything else.

## What problem this solves

Kelly lifted the village-wide brainctl pause on 2026-09-16 ("i think it's
solid") after four independent reviewers verified the FTS/restore-verification
fixes on our fork (`fix/fts-trigger-scope-166-167`, currently at commit
`79fa7e3`). But the actual installed package every agent's MCP server runs
from (`F:\Braincontrol\brainctl`, pointed at by the shared editable install
`E:\python-3-12-10\Lib\site-packages\__editable__.brainctl-2.8.0.pth`) is
still Terrance's original upstream checkout, not our fork. So right now,
lifting the pause delivered nothing -- every agent's live brainctl use still
runs the original, unfixed code. This doc covers what it actually takes to
switch that installed package over safely.

## What's already done (verified, committed, not in question)

1. The FTS/restore-verification fixes themselves: BCTL-18 multi_pass gap,
   self-touch/cold-start rebuild bug, Windows path-normalization bug. Fixed
   across commits `ac34661`, `b3bf2b2`, `62e4f3a`. Independently verified by
   Ari, Grok, Morrow, and me across multiple rounds. Full suite: 2461 passed,
   16 pre-existing unrelated failures, 0 regressions.

2. Found (git status, before touching anything) that the production install
   had 18 modified files of real uncommitted local work, never lost --
   preserved on branch `wip-uncommitted-local-patches-found-20260916`,
   commit `6b0e1bc`, in `F:\Braincontrol\brainctl`.

3. Ported the load-bearing piece of that work into our fork: the
   `BRAINCTL_AGENT_ID` env-var fallback (commit `75a1636`, with new tests
   `TestAgentIdEnvFallback` in `tests/test_mcp_allowed_tools.py`). Without
   it, any agent whose MCP client doesn't pass `agent_id` explicitly gets
   memories silently misattributed to `"mcp-client"` -- the exact bug fixed
   once already in August 2026 (`project_reed_brainctl_repair_2026-08-05.md`).
   Checked every agent's actual current config (not assumed from the old
   report) and found **three** agents depend on this, not just Reed: Ari,
   Grok, and Reed all set `BRAINCTL_AGENT_ID` in their real MCP configs.
   Full suite rerun clean after this port: 2461 passed, same 16 pre-existing
   failures, 0 new regressions.

## What's genuinely open -- the actual thing to decide

### The `brainctl_wrapup` question

While checking agent configs I found Reed's and Grok's real, current
`BRAINCTL_ALLOWED_TOOLS` strings both already list `brainctl_wrapup`. Our
fork only has the old name, `agent_wrap_up`. If production switches to our
fork as-is, `_resolve_allowed_tools()` would hard-exit at startup for both
of them -- their MCP servers wouldn't start.

I ported the rename (commit `79fa7e3`) to fix this. Full suite rerun then
showed 18 failed (up from 16) -- two *new* failures, both from an existing
regression test I hadn't seen before:

```
tests/test_ari_rev2_findings.py::test_r2_b4_agent_wrap_up_tool_name_not_renamed
tests/test_mcp_tools_consolidated.py::test_every_deprecated_v1_tool_has_a_v2_route
```

Reading `test_ari_rev2_findings.py`'s own docstring: this exact rename was
already in this fork once, and Ari's independent adversarial REV2 review
(2026-09-10, `FINDINGS_REV2.md`, referenced at the top of that test file)
caught it, called it an accidental sweep-in from an unrelated branch that
broke this fork's own tool-discovery test, reverted it, and added this test
specifically to keep it from silently coming back.

So there are two real, both-legitimate facts pointing opposite ways:

- **Ari's finding (2026-09-10):** this fork should not carry the rename --
  it's unrelated scope, it came in by accident, and it broke this fork's own
  tests. Reverting it was the right call *for keeping this fork's diff
  minimal and intentional*.
- **What I found (2026-09-16/17), checking live config files directly:**
  Reed's and Grok's real production configs already require the new name.
  Ari's pass doesn't appear to have checked against live agent configs --
  it was about fork hygiene, not deployment compatibility. If we deploy
  without the rename, two agents' servers won't start.

These aren't actually in conflict about the facts -- they're answering
different questions. Ari's question was "does this rename belong in this
fork's diff." My question is "will production work when we point it at this
fork." Both can be true: the rename may not have belonged in the original
accidental sweep-in, and it may still be genuinely necessary now, for a
different, since-confirmed reason (real configs already depend on it).

### What could go wrong with each option

**Option A -- keep the rename (current state, commit `79fa7e3`):**
- Risk: silently overrides Ari's adversarial finding without his sign-off.
  He reviewed this exact change once and rejected it; re-adding it without
  looping him in treats his review as disposable.
- Risk: the two failing tests are guarding against exactly this pattern
  recurring. If I just edit them to pass, I'm suppressing the guard rather
  than resolving the disagreement -- the next accidental sweep-in wouldn't
  be caught either.
- Mitigation if chosen: don't just edit the tests to pass. Update them to
  reflect a *new, understood-and-intentional* state (rename present, with
  its own real justification -- live config compatibility -- documented in
  the test itself), and get that story in front of Ari or Kelly before
  merging further.

**Option B -- revert the rename, keep only the `BRAINCTL_AGENT_ID` fix:**
- Risk: the moment production switches to our fork, Reed's and Grok's MCP
  servers hard-crash at startup (`_resolve_allowed_tools` exits on
  `brainctl_wrapup` as unknown). This is not hypothetical -- I ran their
  exact real `BRAINCTL_ALLOWED_TOOLS` string against the fork and confirmed
  it resolves cleanly only with the rename present.
- Mitigation if chosen: change Reed's and Grok's configs to use
  `agent_wrap_up` instead, so their allowlists match what the fork actually
  exposes. This avoids touching the fork's tool surface at all, but means
  reversing the 2026-08-10 production rename on their end, which propagated
  out for a real naming-clarity reason (`agent_wrap_up` read as ending the
  whole session; it only writes brainctl's own two continuity records).
  Rolling that back for two agents just to avoid a fork conflict feels like
  solving the wrong problem.

**Option C -- ask Ari directly before deciding.** He's the one who reviewed
and rejected this exact change. He may have context I don't (e.g. maybe the
"accidental sweep-in" branch he mentioned is itself worth understanding --
was it importing a change that should have been evaluated on its own merits
and wasn't, or was the rename itself considered and rejected on purpose,
separate from the sweep-in mechanics?). This is the option that treats his
finding as real input rather than an obstacle to route around.

## What I think, stated plainly

Option C, then likely Option A with his input incorporated. The rename
itself looks correct and necessary now, for a reason (live config
compatibility) that's independent of whatever caused the original accidental
sweep-in. But "looks correct to me" isn't the same as having actually
checked with the person whose adversarial finding I'd be reversing. This is
squarely a case for looping in the reviewer, not for me picking a side of a
disagreement I only found by accident while doing something else.

## What's still open after this decision, regardless of which way it goes

1. Finish reviewing the rest of the WIP-preserved uncommitted patches
   (`wip-uncommitted-local-patches-found-20260916`, commit `6b0e1bc`,
   `F:\Braincontrol\brainctl`). Doc/plugin-template wording tweaks for the
   same rename are cosmetic and belong to Reed's upcoming docs pass, not
   this fork. Not yet decided: the June 2026-06-03 `_impl.py` patch
   (verify-and-rebuild-fallback in `cmd_memory_add`) -- see next point.

2. **Separate, larger finding, not yet acted on:** Morrow's connector
   (`F:\GPT-Home\brainctl-connector\server.js`) calls `brainctl.exe`
   directly via CLI with `--agent gpt`, never going through the MCP
   dispatcher (`mcp_server.py`) at all. Every fix verified today --
   BCTL-18, self-touch/cold-start, path-normalization, both agent-identity
   ports -- lives entirely in `mcp_server.py`. The CLI path Morrow uses
   (`_impl.py`'s `cmd_memory_add`) is a separately-implemented function
   with none of that consistency machinery. The June patch is a real, if
   blunt, partial mitigation for a gap in that specific code path. This
   needs its own decision, ideally its own design conversation, not a
   quick port bolted onto this one -- it's new scope, a different
   subsystem, and nobody has adversarially reviewed it the way the MCP
   path was reviewed today.

3. Only after 1-2 are actually resolved: repoint
   `F:\Braincontrol\brainctl`'s working tree to our fork, reinstall the
   editable package, restart MCP server processes, verify agent_id
   resolution and general operation end-to-end (real MCP-protocol test, not
   just isolated logic -- per the 2026-08-05 report's own hard-won lesson
   that restarting the file on disk isn't the same as the running process
   reading it), verify each agent reaches their own `brain.db` correctly,
   then send a follow-up to the village chat since the pause-lifted
   announcement (chat #3292) was made before this whole production-install
   discovery and was technically premature about actual delivery.

4. Separately, Kelly's instruction (2026-09-17): once all of the above is
   settled, pause and hand the how-to-use docs to Reed as documentation
   specialist -- he writes, I review, he revises, repeat until correct. See
   `project_brainctl_docs_with_reed_2026-09-17.md` for the real per-agent
   config differences (TOML vs JSON, tool-cap vs full surface, MCP-env-var
   vs CLI `--agent` flag) the docs need to actually reflect.

## What I'm asking for right now

Whether to loop Ari in directly on just the `brainctl_wrapup` question
(Option C above), or whether Kelly wants to make that call herself. Nothing
else moves until that's settled -- no more edits to the fork, no repointing,
no further test-suite reruns chasing this specific thread.

## Resolution, added 2026-09-28 (11 days later, closing this out)

This sat uncommitted and unresolved in this worktree since 2026-09-17 --
found while resuming Cairn work, not something I circled back to on purpose.
The underlying question got answered organically by unrelated later work,
never through the Option C conversation this doc asked for:

- Separately, on 2026-09-28, the actual production installed package
  (`F:\Braincontrol\brainctl`'s editable install) was repointed from
  Terrance's upstream clone to our real fork
  (`brainctl-dream-cycle-fork`, branch `fix/dream-cycle-time-and-safety`) --
  a distinct, previously-undiscovered gap (production had never been running
  our code at all). That fork's `main` already carried the `brainctl_wrapup`
  rename fully completed (not just ported once, like here) across
  `hippocampus.py`, `mcp_server.py`, and `mcp_tools_consolidated.py`, with
  `_ALL_TOOL_NAMES` correctly unioning `_V2_DEPRECATED` so both names resolve.
  Verified live against all five agents' real configs via `cairn_doctor.py`.
- That confirms this document's own answer (Option A, keep the rename) was
  correct: Reed's and Grok's real `BRAINCTL_ALLOWED_TOOLS` strings do depend
  on the new name, in production, today -- not hypothetically.
- Ari's original REV2 finding is not overridden silently by this: it was
  about an accidental, unreviewed sweep-in from an unrelated branch. The
  rename landing correctly here happened through an explicit, tested,
  independently-verified path (today's WIP review, 102 tests, committed as
  `d6429d3`), not a repeat of the sweep-in Ari caught. Different mechanism,
  same conclusion.

This branch (`fix/fts-trigger-scope-166-167`) still has its own separate,
still-open question: it is unmerged into this repo's `main` (33 commits
ahead as of 2026-09-28), containing the full REV1-18 adversarial-audit chain
for the FTS/restore-verification work. That's tracked as its own item, not
blocked by anything in this document -- this document's own blocking
question is closed.
