# Restore-Time Search Correctness — Design Document

Status: DRAFT, for Ari's review before any implementation. Not yet endorsed by
Kelly. Written 2026-09-14 after Ari's REV12 finding (R12-B1) proved my REV11
characterization of this gap ("cosmetic ordering, membership is guaranteed")
was itself wrong, not just incomplete. This document exists because Kelly
asked directly for a real design pass, reviewed by Ari, before any more code
gets written against this problem — not another patch produced under the
same pressure that led to the overclaim.

## The problem, stated precisely

`tool_memory_search`'s completeness check (`_verify_and_repair_stale_matches`,
fixed across R9-B1 through R11-B1) verifies that returned results are (a) all
genuine real matches, and (b) the *correct count* given the query's limit —
`min(limit, len(real_matches))`. It does **not** verify that the returned
subset is the *correct* subset when the query is limit-truncated and more
real matches exist than slots.

Ari's REV12 reproduction (`test_adversarial_fts_rev12.py`): two real matches
for `sharedneedle`. Row 1 contains it 20 times (the strong match); row 2
contains it once with filler (the weak match). A restore leaves row 1's raw
FTS posting stale (`obsoleteword`) while row 2's stays correct. Real cached
file mtime, size, instance stamp, and ID count/sum are all preserved — no
passive signal detects the restore. With `limit=1`:

- The primary query returns `[2]` — the weak match, because that's the only
  one with a matching raw posting.
- The completeness check sees: 1 result, it's a genuine real match (verified
  via the eligible-scan), `min(1, 2) == 1` results expected. Check passes.
  No rebuild. No failure flag.
- The actual best answer (row 1) is silently never returned. `ok: true`,
  full expected count, nothing to tell a caller anything is wrong.

**This is a real correctness gap, not a display/ordering nicety.** A caller
asking "what's my most relevant memory about X" can get a genuinely worse
answer than exists, indistinguishable from a correct one, for as long as the
restore goes undetected by every other passive layer in this file (which,
per R8-B1/R9-B1's own findings, can be indefinitely for an adversarial or
unlucky restore).

## Why the cheap fix doesn't work (and what "cheap fix" means here)

The obvious next step — extend the existing disposable, query-scoped verify
table to also check rank order, not just membership — is what I tried and
rejected before REV12, and Ari's response correctly separated two different
claims I'd conflated:

1. **"This specific approach doesn't work"** — true, and the actual reason
   matters: SQLite FTS5's default `rank` (bm25) is computed from corpus-wide
   statistics (average document length, term document frequency) across the
   *entire* indexed table. The verify table this mechanism builds only ever
   contains the current query's WHERE-scoped eligible rows — a different,
   usually much smaller population than the real index's full corpus.
   Different population → different bm25 statistics → rank comparisons
   between the two are not meaningful, and would produce real false
   positives (legitimate rank differences from differing corpus stats,
   flagged as corruption) on top of whatever real staleness exists.

2. **"Therefore the whole problem is unfixable this way"** — Ari's correct
   objection: (1) doesn't establish (2). It only rules out the *specific*
   implementation of building a scoped mini-corpus. It says nothing about
   whether some other implementation could verify against the *right*
   corpus, or avoid needing corpus statistics at all.

I'd collapsed these into one conclusion under time pressure and shipped a
narrower guarantee than I described. That's the actual mistake REV12 caught,
not just a missing feature.

## Candidate directions

### A. Full-corpus verification

Build the disposable verify table from the *complete* population the real
`memories_fts` index computes its rank statistics over — not just this
query's eligible subset — so bm25 comparisons are meaningful.

**Measured, not left open:** `memories_fts` is a single shared virtual
table, not partitioned per-agent, so bm25 statistics are computed across
*every* agent's indexed, non-retired content. Checked the real production
corpus directly (read-only, `F:\brain\brain.db`, admin-diagnostic access,
no write): 213 total memories, 212 eligible (indexed, not retired), across
6 distinct agents. The whole database file is 4MB. At this real scale,
rebuilding a full disposable verify table from the *entire* corpus on every
verification call is trivially cheap — not the "large, ongoing cost" I'd
assumed before checking. This reopens option A as genuinely viable today,
not just architecturally correct-but-expensive.

**What's still actually open:** this measurement is a snapshot, not a
scaling guarantee. 212 rows across 6 agents is the household's real current
size; if that grows by an order of magnitude or more, "reindex everything
every search" stops being free. Worth deciding now whether to build (A)
as the honest, currently-cheap fix and revisit cost if/when the corpus
actually grows, versus over-engineering for a scale that may never arrive.
My own inclination, not yet tested against anyone else's judgment: build
for the real 212-row scale now, not a hypothetical one.

### B. Authoritative restore/invalidation contract

Every fix in this file so far (R9-B1 through R11-B1) has been a *passive
detection* layer, reactive to a restore already having happened, because —
as R9-B1's own docstring already noted — **no actual in-process restore
command exists in this codebase today.** Restores currently happen entirely
out-of-band (file copy, backup-restore script, whatever operational tooling
does it), which is exactly why passive detection is the only lever available
at all.

If a real, in-process restore/import path existed and was *required* to call
a full invalidation (`invalidate_fts_check_cache()` already exists for this
purpose, per R7-B1's own docstring — "the actual caller and tested path...
any future restore tooling... should call it as its last step") — the gap
closes at its actual source. Passive detection becomes defense-in-depth
against restores that bypass the contract (a bug, an operator using raw
file tools instead of the sanctioned path), not the sole correctness
mechanism it's currently being asked to be.

**Checked, not left open:** grepped the full source tree for any
restore/import/backup entry point that could call `invalidate_fts_check_cache()`
-- none exists. It's defined, documented as "the actual caller and tested
path" a future restore tool should use, and has zero callers anywhere in
this codebase. So (B) is not "wire up an existing path" -- it's "design and
build a real in-process restore/import command from scratch" (what does
"restore" mean operationally here -- a full DB file replace, a selective
memory import, both? who/what calls it, a human operator, a scheduled job,
a recovery script?), which is real new scope, not a quick fix either.

### C. Explicit degradation signal, no attempted fix

Accept that neither A nor B is available immediately. Instead of silently
returning `ok: true` with a wrong-but-plausible result, detect *only* the
specific R12-B1 shape (more real matches exist in the eligible set than were
returned, even though the count and membership checks both pass) and
surface a new flag — something like `possible_rank_omission: true` — telling
the caller "a truncated result was returned; correctness of *which* results
survived truncation could not be verified, only that they're real." This is
detectable cheaply: it's just "does `len(real_ids) > expected_count`", no
rank computation needed at all — the case IS already distinguishable from
ordinary complete truncation, I just wasn't surfacing it as a distinct signal.

This doesn't fix the omission. It converts a silent wrong answer into a
flagged uncertain one — the same shape as the `index_verification_failed`
and `index_repair_failed` flags already in this file for other unresolvable-
this-call situations. Cheapest option, ships fastest, closes zero actual
data-loss, but stops the specific failure mode Ari named ("full result
count and no failure flag") from being silent.

## What I'd recommend, but am not deciding

Updated after actually measuring the real corpus (212 eligible rows, 6
agents, 4MB file): I'd now recommend (A), full-corpus verification, as the
real fix, not just a someday direction — at this scale it's cheap, and it's
the only option of the three that actually closes the omission rather than
flagging or proceduralizing around it. (C)'s cheap flag is still worth
shipping *alongside* A, not instead of it: even a correct full-corpus
verifier can itself fail to run (denied connection, same R10-B3 shape),
and that failure should be visible the same way it already is elsewhere in
this file, not silent. (B) — a real restore contract — is worth building
separately regardless of which detection approach wins, since detection-
after-the-fact will always be a second line of defense, not a substitute
for closing the gap at the point staleness is introduced; but it's real new
scope (no restore path exists to wire into today) and shouldn't gate A or C.

I'm more confident in this than my first-pass recommendation, because it's
now grounded in an actual number instead of an assumption I hadn't checked.
Still Ari's and Kelly's call, not mine to finalize — in particular I haven't
prototyped A against the real REV12 fixture yet to confirm it actually
closes the gap in practice, only reasoned that it should.

## What this document is not

Not a fix. Not committed anywhere. Not something I'm treating as agreed.
Written because Kelly asked for a real design pass before more code, and
because Ari's REV12 report asked, directly: "state the actual tradeoff...
whether this is accepted, how long it can persist, and what explicit restore
action prevents it" — this is my attempt to actually answer that honestly,
not a way to route around answering it.
