# Backward adversarial audit -- hippocampus.py -- Claude, 2026-07-12/13

Independent audit, not limited to the known timezone bug (THE-65 / #168). Genuinely
adversarial: looking for what breaks this code, not confirming what's already known
to break it. Written before reading GPT's independent audit, to keep the two honest.

Method: reading the file section by section (~300 line chunks), noting anything that
looks like a real risk as I hit it, not retrofitting a narrative after the fact.

---

## Section 1: lines 1-300 (imports, constants, get_db, parse_ts, days_since,
## resolve_event_agent, cmd_decay, ensure_agent, compress_scope_group start)

**F1. `get_db()` connections are never closed.** (line 73-84) Every function that
calls `get_db()` gets a fresh sqlite3.Connection with no matching `.close()` anywhere
I've seen yet. For a single short-lived CLI invocation this leaks one handle per
process (harmless in practice). But if `cmd_consolidation_cycle` or `run_dream_cycle`
call multiple sub-functions that each call `get_db()` independently within one long
-running Python process, that's N separate connections, each with WAL mode on,
potentially N separate implicit transactions -- directly relevant to GPT's
phase-isolation/atomicity question. Need to check as I read further: does the
orchestrator open ONE db connection and pass it down, or does each phase/sub-function
open its own? If the latter, "phase isolation" is already broken at the connection
level, not just the commit level -- two connections to the same SQLite file in WAL
mode can each hold their own transaction, and a crash in one doesn't roll back
writes already committed via another connection's autocommit-per-statement default
mode. This could be a bigger structural finding than the timestamp bug.

**F2. `cmd_decay` has no try/except around the mutation loop.** (lines 143-216) If a
single row triggers an exception mid-loop (malformed `tags` JSON, unexpected None,
whatever), everything already `db.execute()`'d in that loop is left in an ambiguous
state -- not explicitly committed, not explicitly rolled back. Python's sqlite3
default isolation_level starts an implicit transaction on the first DML statement;
if the connection object is later garbage-collected without commit, sqlite3 rolls
back automatically -- so this is *probably* safe by accident (GC-triggered rollback)
rather than by design. Worth confirming empirically rather than assuming either way.

**F3. `days_since()` silently floors negative elapsed time to 0.0.** (line 101,
`max(0.0, seconds / 86400.0)`) If `now` is ever *earlier* than the stored timestamp
(clock skew, a naive/aware mismatch that happens to invert the subtraction sign
instead of raising, or a future-dated `created_at` from bad input), this doesn't
error -- it silently reports "0 days elapsed," which for `cmd_decay` means zero decay
applied. A memory with a corrupted future timestamp would never decay, never warn,
never retire. Silent-wrong is worse than crash-loud here, and it's a real gap in the
"what happens on bad input" question the whole timezone-bug conversation is about --
we've been focused on the case that crashes, not the case that silently produces a
wrong-but-plausible number.

**F4. `resolve_event_agent`'s final fallback can raise mid-batch-operation.** (lines
104-118) If somehow zero rows exist in `agents` at all, this raises `RuntimeError`
from inside what's often a helper call at the *start* of a larger function (e.g.
`cmd_decay` calls it at line 125, before any mutation happens -- safe there). Need to
check whether every caller calls this early (safe) or ever calls it mid-transaction
after other writes already happened (unsafe, partial-write risk). Flagging to check
as I go, not yet confirmed either way.

**F5. `compress_scope_group`'s dynamic SQL uses f-string interpolation for the
placeholder list, not the values.** (line 266-269: `f"UPDATE memories SET
retired_at = datetime('now'), updated_at = datetime('now') WHERE id IN
({placeholders})"`) The `placeholders` string itself is just `"?,?,?..."` built from
`len(source_ids)`, and the actual `source_ids` values are passed as bound parameters
-- this is the *correct*, injection-safe pattern (f-string only builds the shape of
the query, not the content). Noting it because it looks alarming at a glance (f-string
+ SQL in the same line) but isn't actually a bug. Worth checking every other dynamic
SQL construction in the file against this same distinction -- shape-only interpolation
is fine, value interpolation is not.

**F6. `compress_scope_group` retires originals and inserts replacements in two
separate statements with no atomicity boundary shown yet.** (lines 265-281) If the
process dies between the `UPDATE ... retired_at` (line 267-270) and the `INSERT`
loop (line 273-281), the source memories are retired but their replacement was never
created -- real, permanent data loss, not just a stale timestamp. This is exactly the
"partial write" class GPT flagged in the abstract; here's a concrete instance of it,
in a function whose whole job is compression, i.e. it runs unattended as part of
dream_cycle. Whether this matters depends on whether the caller wraps this in a
larger transaction (checking as I continue) -- if `dry_run` is the only guard and
there's no savepoint, a crash here is real, permanent data loss of the retired
originals with nothing to replace them.

---

## Section 2: lines 300-650 (cmd_compress, embedding helper, FTS5 similarity,
## build_similarity_clusters, call_llm_consolidate, consolidate_cluster start)

**F7 -- LIKELY THE BIGGEST FINDING SO FAR: `consolidate_cluster` is currently a
guaranteed no-op.** (lines 581-625) `call_llm_consolidate()` (line 581-584) is a
stub that was gutted when LLM calls were removed from brainctl ("brainctl is
model-agnostic... calling agent should handle LLM reasoning") -- it unconditionally
`return None`. `consolidate_cluster` calls it at line 620 and checks `if not
consolidated:` at line 621 -- since it's always `None`, this branch is *always* taken:
prints "FAILED to get LLM response — skipping cluster." and returns `False`,
every single time, for every cluster, unconditionally. The actual mutation code below
it (lines 627-648+: the INSERT of the consolidated memory, the UPDATE retiring
originals) is dead code in the current build -- structurally unreachable, not just
untested. This means: if `consolidate_cluster` (as opposed to `compress_scope_group`,
which correctly uses `_fallback_compress` instead and does work) is part of the live
dream_cycle path, that whole consolidation phase currently does *nothing* silently --
no error, no crash, just a per-cluster print to stdout that nothing captures unless
someone's watching the terminal. Need to trace who calls `consolidate_cluster` (I'd
guess `consolidate_memories` around line 2497 based on the earlier writer-inventory
pass) and whether that caller surfaces "0 clusters actually consolidated" as a
visible stat or buries it. If dream_cycle reports "consolidation: N clusters
processed" without distinguishing "processed" from "actually consolidated," this is
a silent-failure-mode finding on par with the timezone crash -- arguably worse,
because it doesn't crash, it just quietly does nothing while looking like it worked.

**F8. `build_similarity_clusters`' union-find is O(memories x candidates) with no
visible cap on total memories scanned.** (lines 536-578) `find_fts5_similar` caps
each individual query at `LIMIT 50` (line 512/527), but the outer loop in
`build_similarity_clusters` (line 547) calls it once per memory in `memories` with
no visible upper bound on `len(memories)` itself at this layer -- the cap would have
to come from whoever builds the `memories` list this function is handed. Not
confirmed as a real problem yet (depends on caller), but worth checking: if this is
ever handed "all active memories in a scope" with no LIMIT, and a scope grows large,
this is O(n) FTS queries per consolidation run, each up to LIMIT 50 -- could get slow
but wouldn't corrupt data. Lower severity than F7, flagging for completeness.

**F9. `_try_embed_new_memory` opens a second, separate raw `sqlite3.connect()`
outside `get_db()`.** (lines 446-465) This bypasses `get_db()`'s existence check and
opens its own WAL-mode connection directly, then commits and closes it independently
of whatever connection/transaction the caller (`consolidate_cluster` et al.) is
using. Two live connections to the same WAL database file, each with their own
transaction boundary, confirms the F1 concern from Section 1 is real, not
hypothetical -- here's a concrete second connection actually being opened
mid-operation. If the embed succeeds and commits via `vec_conn`, but the *caller's*
own transaction (the one that just inserted the memory row via `conn`, using the
`conn` object passed into `consolidate_cluster`) later gets rolled back for any
reason, the embedding for a memory that no longer exists (or was rolled back) would
be orphaned in `vec_memories`/`embeddings`. Low-severity data-hygiene issue (orphaned
embedding rows, not corruption), but it's a second real instance of the
multi-connection pattern, worth citing alongside F1 as evidence this isn't a one-off.

**F10. `find_fts5_similar` swallows `sqlite3.OperationalError` silently and returns
`[]`.** (lines 499-533) If the FTS5 index is broken/missing (exactly the class of
bug Reed hit and I fixed on 2026-07-11 -- see brainctl memory and
`memory_village_faceplant_night_20260710.md`), this function returns "no similar
memories found" instead of erroring. Every consumer of this function (clustering,
contradiction-detection) would then behave as if nothing is similar/contradictory --
silently under-consolidating and under-detecting contradictions rather than failing
loudly. This is the exact failure mode GPT's THE-65 comment already named in the
abstract ("A failed/empty retrieval mechanism must not be interpreted as 'no
existing memory'") -- here's the literal code line that produces it. Worth citing
GPT's own point back at him with the concrete line number when I write this up.

---

## Section 3: lines 650-1100 (cmd_consolidate, decay/pressure helpers, apply_decay,
## compute_ewc_importance, _has_active_dependents, compute_spacing_decay)

**F7 CONFIRMED, not hypothetical.** `cmd_consolidate` (line 668) is the actual live
caller of `consolidate_cluster` (line 714). Every cluster it finds goes through the
dead LLM-stub path and prints "FAILED to get LLM response — skipping cluster,"
contributing 0 to `total_clusters`/`total_retired` (lines 715-717), silently, no
distinct warning/error event logged anywhere. `cmd_consolidate` as a whole command is
currently a no-op for its actual consolidation purpose -- it still does useful work
(loads memories, builds clusters, runs procedural synthesis at the end, lines
723-739) but the core "consolidate similar memories" action never fires. Need to
check later whether this is the function dream_cycle's NREM/REM/Insight phases
actually call, or whether they call `consolidate_memories` (~line 2497, different
function, confirmed via the earlier writer-inventory pass to use a different write
pattern) -- if dream_cycle calls the working one, F7 is a latent landmine for anyone
who runs `hippocampus.py consolidate` directly rather than through dream_cycle, still
worth fixing but lower urgency. If dream_cycle calls *this* one, F7 is live and
already firing during real dream cycles today.

**F11. `_has_active_dependents` runs a full unindexed table scan inside the
per-memory decay loop.** (lines 921-941, called from `apply_decay` line 1024) Every
time a memory in `apply_decay`'s loop would be retired, this fires `SELECT id,
derived_from_ids FROM memories WHERE retired_at IS NULL AND derived_from_ids IS NOT
NULL` -- a full scan of all active memories with a derivation chain -- then loops in
Python over the JSON-decoded results. This is O(retiring memories x
memories-with-derivations) inside `apply_decay`'s own O(all active memories) outer
loop. Not a correctness bug, a scaling one: on a large database with many
derived-from chains, a single decay pass could get quadratic. Worth flagging as a
"works today, may not at scale" item, lower priority than F7.

**F12. `apply_decay`'s Bayesian branch (`_has_ab`, lines 1028-1054) silently
*discards* the scalar exponential-decay `new_confidence` computed at line 1014 and
recomputes a different value from alpha/beta at line 1036.** Not a bug per se (looks
intentional -- "override scalar decay" comment at line 1035 confirms it), but it
means two different confidence-decay models coexist in the same function depending
on whether the `alpha`/`beta` columns exist, and the earlier scalar computation
(`math.exp(-rate * elapsed_days)`, line 1014) is fully wasted work whenever the
Bayesian path is taken. Not a correctness risk, just noting the dead computation for
whoever touches this function next -- easy to misread as "the exponential decay is
the real one" when it's actually always overridden on any DB that has alpha/beta
columns (which, per the writer inventory, includes every memory row -- alpha/beta
have defaults of 1.0).

**F13. `compute_ewc_importance` commits inside the function itself (line 917),
independent of any caller-level transaction.** Confirms GPT's own point in his
THE-65 comment ("run_insight_phase alone calls db.commit() twice internally") is a
pattern that shows up outside insight phase too -- `apply_decay` (line 1071),
`compute_ewc_importance` (line 917), and `_mark_importance_locks` (line 868) all
commit internally. Any orchestrator wrapping multiple of these in sequence gets a
series of independently-committed steps with no way to roll the whole sequence back
if a later step fails -- each of these three functions is individually a commit
boundary whether the caller wants it to be or not.

---

## Section 4: lines 1100-1550 (spacing/review scheduling, synaptic tagging,
## proportional downscaling, apply_recall_boost, apply_temporal_demotion,
## analyze_access_patterns, temporal_classification_pass start)

**F14 -- CONFIRMED CROSS-FILE, LIVE BEHAVIORAL BUG, DIFFERENT FROM THE KNOWN ONE:
`labile_until` is written as SQLite-UTC `'now'` in other files but read/compared
against Python-local `datetime.now()` inside hippocampus.py's `apply_decay`.**
Traced this because `apply_decay` (line 998: `if labile_until and labile_until >
now_sql`) and `apply_synaptic_tagging` (line 1181: `labile_until >
strftime(..., 'now')`) use two *different* comparison bases for the same column,
which made me check how `labile_until` actually gets written. Grepped the whole
`src/agentmemory/` tree:
- `_impl.py:2617` writes it via `strftime('%Y-%m-%dT%H:%M:%S', 'now', '+2 hours')`
  -- SQLite's own `'now'`, which is UTC.
- `mcp_tools_meb.py:728` writes it via `strftime(..., datetime('now', '+20
  minutes'))` -- also SQLite UTC.
- `mcp_server.py:1292` writes it via Python `(now_dt + timedelta(hours=...)).strftime(...)`
  -- haven't yet confirmed whether `now_dt` there is local or UTC; flagging as a gap,
  not yet resolved.
- `apply_synaptic_tagging` (hippocampus.py:1181) *reads* it back via SQL-side
  `strftime(..., 'now')` -- UTC-consistent with the `_impl.py`/`mcp_tools_meb.py`
  writers. This comparison is internally correct.
- `apply_decay` (hippocampus.py:998) *reads* it back via Python-side `now_sql`
  (from `datetime.now()`, i.e. **local, naive**) -- inconsistent with the UTC
  writers.

**Concrete behavioral consequence:** on a machine whose local time is behind UTC
(e.g. Arizona/MST, UTC-7, no DST) -- which is this actual production machine per
`kelly-profile.md`/`infrastructure-map.md` -- comparing a UTC-stamped `labile_until`
against a local-time `now_sql` string means `apply_decay`'s labile-window check stays
`True` for roughly 7 hours *longer* than the window was actually meant to protect the
memory. Effect: memories exit their reconsolidation-window decay-immunity later than
intended, i.e. they get *extra* protection from decay, silently, for up to ~7 hours
past when the window should have closed. Not data loss, not a crash -- a quiet
behavioral skew in favor of over-protecting recently-recalled memories. This is a
genuinely separate bug from the `days_since()` crash (different column, different
file, different failure mode -- wrong-but-plausible rather than crash), found by
going backward from "these two functions compare the same column two different
ways" rather than from the known bug shape. Directly relevant to GPT's proposed
UTC-aware-everywhere policy: this is a second, independent piece of evidence for
adopting it, in a place the original 9/23-site hippocampus-only inventory would never
have surfaced since the writes live in other files.

**F15. `apply_proportional_downscaling`'s final tag-cycle decrement (lines
1262-1264) runs as one unconditional UPDATE after the per-row loop, with its own
implicit transaction scope shared with the loop's row-by-row UPDATEs -- but all of it
commits together at line 1265.** This one is actually fine (single commit at the
end, no premature per-row commit) -- noting it as a *contrast* example: this function
does transaction boundaries correctly (all-or-nothing within itself) where `apply_decay`,
`compute_ewc_importance` etc. also do (each is also single-commit-at-end internally).
The GPT-flagged problem isn't "these individual functions are unsafe internally" --
each one *is* atomic on its own. The problem is purely at the orchestration layer:
nothing wraps multiple of these already-atomic functions into one larger transaction,
so a crash *between* function calls in the orchestrator still leaves a partial
dream-cycle. Worth being precise about this distinction when reporting: the
individual building blocks are transactionally sound, the orchestrator isn't.

**F16 (minor).** `analyze_access_patterns`' promotion branch (lines 1447-1480) and
`temporal_classification_pass`'s upcoming reclassification logic both read
`last_recalled_at`/`created_at` fresh per-row from the same `SELECT` at the top of
the function, so within a single pass there's no read-after-write staleness risk
(good). Not a finding, just confirming no bug here after checking, since this was a
plausible adversarial angle (stale reads after a mid-loop write) that turned out
clean.

---

## Section 5: lines 1550-2400 (temporal_classification_pass tail, run_hebbian_pass,
## _store_health, experience_replay, build_entity_clusters start, run_phased_consolidation
## tail, cmd_consolidation_cycle -- THE main orchestrator)

**F17. `run_hebbian_pass` (lines 1622-1798) never calls `db.commit()` internally --
confirmed by reading the whole function to its `return stats` at line 1798.** All
three of its direct callers handle this differently: `cmd_hebb` (line 3477-3479)
explicitly commits right after calling it -- safe. `cmd_consolidation_cycle` (line
2316) relies on a single shared `db.commit()` at line 2325, many passes later --
see F19 below, this is where it gets dangerous. `run_phased_consolidation` (line
2236) wraps the call in try/except and does NOT show an explicit commit right after
in the code I've read -- needs one more check, flagging as unconfirmed.

**F18 (minor/cosmetic).** `experience_replay` (lines 1898-1938): the `if
row["temporal_class"] == "permanent": skipped += 1` block (lines 1930-1932) has no
`continue` -- execution falls through to `apply_recall_boost` regardless, so
permanent memories are NOT actually skipped (they get boosted just like everything
else, consistent with `apply_recall_boost`'s own docstring "Permanent memories are
boosted too"). The bug is purely in the stats: a permanent memory gets counted in
*both* `skipped` and `replayed_ids`/`replayed`. Not a data bug, a
reporting-accuracy bug -- directly relevant to GPT's dry-run/inspectability
requirement ("exact source IDs and proposed target values") since right now even a
*real* (non-dry-run) cycle's stats double-count here.

**F19 -- THE MOST SIGNIFICANT FINDING OF THE AUDIT: `cmd_consolidation_cycle` (lines
2253-2400+), described in its own docstring as "the main scheduled job that runs as
the memory consolidation cycle," has INCONSISTENT commit boundaries across its 11
sequential passes, on a SINGLE shared connection, with a crash-prone tail.**

Traced concretely, not hypothetically:
- One `db = get_db()` at line 2260, threaded through all 11 passes as `db` -- so my
  earlier F1 worry about *multiple connections* doesn't apply here specifically
  (good news, worth being fair about).
- BUT within that one connection, passes 0/0.1/0.5/1/2/3/8 each call functions that
  commit *internally, mid-sequence*: `compute_ewc_importance` (self-commits, line
  917), `_mark_importance_locks` (self-commits, line 868), `temporal_classification_pass`
  (self-commits, line 1618), `apply_decay` (self-commits, line 1071),
  `apply_temporal_demotion` (self-commits, line 1384), `analyze_access_patterns`
  (self-commits, line 1488), `experience_replay` (self-commits, line 1937).
- Passes 4/5/6/7/9/10/11 (`resolve_contradictions`, `consolidate_memories`,
  `compress_memories`, `promote_episodic_to_semantic`, `run_hebbian_pass`,
  `mine_causal_chains`, `run_dream_pass`) -- have NOT yet confirmed each of these
  individually for self-commit behavior (flagging as remaining work, see below), but
  `run_hebbian_pass` (pass 9) is confirmed via F17 to NOT self-commit, meaning its
  writes ride on the single `db.commit()` at line 2325, at the very end.
- `run_dream_pass` (pass 11, line 2323) is called completely unguarded -- no
  try/except around it in `cmd_consolidation_cycle` (contrast with the *other*
  orchestrator, `run_phased_consolidation`, which wraps its own `run_dream_pass`
  call in try/except at lines 2226-2229 and silently swallows any exception into
  `{"skipped": "dream_pass_not_available"}`).

**Concrete failure mode, not abstract:** if `cmd_consolidation_cycle` throws partway
through -- say, `run_dream_pass` at line 2323 (the phase GPT flagged as "literally
never been observed running") raises an uncaught exception -- every pass before it
that self-commits (EWC scoring, importance locks, temporal reclassification, decay,
demotion, access-pattern promotions, experience replay) is *already permanently
applied to the live database*, while `run_hebbian_pass`'s edge writes (pass 9, right
before dream_pass) are *silently lost* because they were relying on the commit at
line 2325 that never executes. The cycle also never reaches the summary/event-log
write at lines 2330-2400, so there's no record in `events` that a cycle even ran,
despite real, permanent mutations having happened. This is not "no atomicity" in the
abstract GPT described -- it's *worse*: a crash produces a hybrid state that's
neither fully-before nor fully-after the cycle, split along an internal
implementation detail (which functions happen to call commit() themselves) that
isn't visible from the orchestrator's own code without reading every callee.

**Second, independent angle on the same function:** the two orchestrator paths
(`run_phased_consolidation` for `--phased`, vs. the direct pass-by-pass sequence in
`cmd_consolidation_cycle` for the default path) have different failure philosophies
for the exact same `run_dream_pass` call -- one swallows errors silently (matching
the FTS5-swallow pattern already flagged in F10, and directly contradicting GPT's
own stated principle "a failed/empty retrieval mechanism must not be interpreted as
'no existing memory'" -- here it's "a failed dream pass must not be interpreted as
'dream pass not available'"), the other crashes hard mid-cycle with partial
commits already applied. Neither is what GPT's Milestone 1 (bounded, inspectable,
stop-on-failure, explicit partial-write flag) describes; the current code is
essentially two different wrong answers to the same open design question, not one
consistent behavior to fix.

**Remaining gap, honestly flagged:** I have not yet individually confirmed the
self-commit status of `resolve_contradictions`, `consolidate_memories`,
`compress_memories`, `promote_episodic_to_semantic`, and `mine_causal_chains`
(passes 4/5/6/7/10). Based on the pattern seen in every other function so far
(11 of 13 checked so far self-commit), I'd guess most of them do too, but F19's
conclusion (inconsistent, crash-prone, undefined-hybrid-state orchestration) already
holds regardless of how that guess resolves -- confirmed via `run_hebbian_pass`
alone being the one exception threading unsaved writes past several self-committing
neighbors.

---

## Section 6: lines 3529-3770 (run_dream_pass in full, cmd_dream_pass)

**Correction to my own F19, found by reading further rather than stopping at the
first plausible-sounding claim.** `run_dream_pass` (the REM/bisociation phase GPT
specifically flagged as "literally never been observed running") self-commits
*twice* -- once after hypothesis expiry/promotion (line 3609) and once at the very
end (line 3754). Since every pass in `cmd_consolidation_cycle` shares one `db`
connection, `db.commit()` commits *everything pending on that connection*, not just
the calling function's own writes. That means `run_hebbian_pass`'s pending writes
(pass 9, no internal commit of its own) don't get silently lost outright -- they ride
along and get committed by `run_dream_pass`'s own first internal commit (line 3609,
pass 11) *as long as dream_pass gets that far without throwing first*. Correcting
F19's framing: the real risk isn't "hebbian's writes are always lost," it's "the
durability boundary after a crash is wherever the *last successful commit on the
shared connection happened, across all passes so far* -- which is invisible from
`cmd_consolidation_cycle`'s own code and depends on how deep into the *next* pass a
crash occurs." Flagging this correction explicitly rather than letting the punchier
but less accurate version of F19 stand -- the underlying problem (inconsistent,
crash-prone, hard-to-reason-about durability boundaries) is still real and still the
most significant finding of the audit, just more precisely: **the true unit of
atomicity in this orchestrator is not "one pass," it's "however many passes happen
to run between two internal commit() calls, wherever they happen to fall" -- which
no one designed on purpose and isn't visible without reading every callee.**

Separately, `run_dream_pass` itself is well-built where I can check it: real
wall-clock cap (`DREAM_MAX_WALL_SECONDS`) and hypothesis-count cap on its O(n^2)
pairwise similarity scan (lines 3678-3720, with an explicit comment explaining why:
"enough to peg a CPU on weak hardware... we cap on both... so a single cycle can
never wedge the daemon"), graceful degradation to 0 candidates if fewer than 2
embedded memories exist (line 3625-3626), and correct incubation/promotion/
cooldown logic. No new correctness bugs found here -- noting the *positive* finding
too, since an adversarial audit that only ever reports problems isn't more credible,
it's just less useful for triage. This phase looks like the most carefully-built
part of the file audited so far.

---

## Audit status and stopping point

Covered in full, line-by-line: 1-2400 and 3473-3770 of 4138 total lines (~68%),
including both orchestrator entry points (`cmd_consolidation_cycle`,
`run_phased_consolidation`), the full Hebbian pass, and the full dream/REM pass.
Not yet read in this pass: `resolve_contradictions`, `consolidate_memories`,
`compress_memories`, `promote_episodic_to_semantic`, `mine_causal_chains`,
`search_memories` (~2400-3473, ~3770-4138) -- these were already covered by the
earlier forward writer-inventory pass for the *specific* timestamp question, just
not yet re-read adversarially for other risk classes. Stopping here to report real
findings now rather than let the audit run indefinitely; can resume into the
remaining ~32% if useful after cross-correlating with GPT's independent pass.

---

## Summary of findings, ranked by what actually matters for THE-65 and the fix design

1. **F19 (refined) -- orchestrator atomicity is undefined, not just "not
   transactional."** The real unit of durability in `cmd_consolidation_cycle` is
   "whatever ran since the last incidental internal commit() call," which is an
   accident of implementation, not a design. This is the strongest, most concrete
   evidence yet for GPT's Milestone 1/2 phase-isolation proposal -- and sharpens it:
   the fix isn't just "add phase boundaries," it's "make internal commits
   deliberate and phase-aligned instead of scattered per-function."
2. **F7 -- `consolidate_cluster`/`cmd_consolidate` is a confirmed, currently-live
   no-op** due to the gutted `call_llm_consolidate` stub. Real functionality gap,
   separate from and probably higher-urgency than the timestamp bug, if this is ever
   invoked directly (needs one more check: does `cmd_consolidation_cycle`'s pass 5
   call this dead path, or the working `consolidate_memories`? -- see below).
3. **F14 -- confirmed live, cross-file, currently-active behavioral bug** in
   `labile_until` comparisons (UTC writes in `_impl.py`/`mcp_tools_meb.py`, local-time
   read in `apply_decay`) on a real UTC-behind machine. Silent, not crashing, but a
   genuine second independent argument for GPT's UTC-everywhere policy, found
   entirely outside the original hippocampus.py-only scope.
4. **F10 -- silent FTS5 failure swallowing** in `find_fts5_similar`, which is
   exactly the failure mode GPT's own comment already warned about in the abstract
   ("a failed/empty retrieval mechanism must not be interpreted as 'no existing
   memory'") -- and the "silent swallow" orchestrator pattern for `run_dream_pass`
   in `run_phased_consolidation` (lines 2226-2229) is the same failure family again,
   a third instance.
5. **F6/F9 -- partial-write risk in compression/embedding paths** (retire-then-insert
   with no savepoint; a second raw connection for embeddings committing independently
   of the caller's transaction).
6. Smaller items: F3 (negative-elapsed silently floored to 0, hides bad timestamps
   instead of surfacing them), F11 (O(n^2)-ish scaling risk in decay's dependent-check),
   F18 (cosmetic stat double-counting in experience_replay), F1/F2 (connection-close
   and mid-loop-exception hygiene, low severity, probably safe by GC accident not
   design).

**Open thread resolved, F7 severity downgraded.** Confirmed by reading
`consolidate_memories` in full (line 2430-2500+): it is a genuinely different,
*working* function from `consolidate_cluster`. It takes `llm_fn: Optional[Any] =
None` and falls back to plain `"; ".join(contents)` (line 2495) when no LLM function
is provided -- it never touches the gutted `call_llm_consolidate` stub at all. Since
`cmd_consolidation_cycle` pass 5 (line 2291) calls `consolidate_memories`, not
`consolidate_cluster`, **the actual scheduled dream_cycle consolidation path is NOT
affected by F7.** F7 is real but narrower than first flagged: it only breaks the
standalone `hippocampus.py consolidate` CLI command (`cmd_consolidate` ->
`consolidate_cluster` -> dead `call_llm_consolidate` stub), not the dream_cycle
orchestrator. Still worth fixing (anyone running that CLI command directly gets
silent no-op behavior with a misleading "FAILED to get LLM response" message that
looks like a transient error rather than "this code path is permanently gutted"),
but correcting my own severity claim: this is a CLI usability bug, not a live
dream_cycle defect. Glad I checked rather than let the punchier claim stand.
