# Restore-Time Search Correctness — v4 Amendment (closes V3-1, V3-2, V3-3)

Status: DRAFT v4, small consistency amendment to v3 (SHA256
`ae92fdbc21ab34a4633f2bddb41b07d160c5c0bb475dd734c8b81d5049b500bf`) and v2
(SHA256 `7c9b3ef66b93c9a0704b356e26ad4bd6ec1d3298d654ffab6565a6a8c54e05fd`).
All prior accepted design choices (direction A, D1's algorithm, D3's
predicate, D4's scale/entry-point findings, V2-1's ownership split, the
3-attempt bound, savepoint-around-repair-and-verification) remain settled
and are not reopened. This closes exactly the three contradictions/gaps
Ari's REV3 named, using his own compact flow as the authoritative sequence
rather than re-deriving one.

## V3-1: one authoritative finalization rule

v3's prose said "roll back on unrecoverable outcomes" in one place and the
state table said "commit" for several of those same outcomes — a real,
direct contradiction, not two views of the same thing. Corrected, single
rule, no exceptions:

- **Verified success** → savepoint released (if one was open) → **commit**
  if this operation owns the outer transaction.
- **Any unsuccessful outcome** (verification failed, repair failed,
  post-repair comparison still disagrees, snapshot retries exhausted) →
  savepoint rolled back (if one was open) → **roll back** the outer
  transaction if this operation owns it.
- **Caller-owned outer transactions are never finalized here, in either
  direction** — no commit, no rollback, regardless of outcome. Only the
  savepoint (fully within this operation's control) is resolved; the
  caller's own transaction is untouched either way.
- **Cleanup/commit/rollback themselves can fail** (a `COMMIT`/`ROLLBACK`
  statement can itself raise) — this is now an explicit additional failure
  state, not assumed to always succeed: **never return a verified-success
  result before the owning transaction has actually, successfully
  finalized.** If finalization fails after a verified comparison, the
  correct comparison result is real but the operation's own bookkeeping
  isn't — return `index_verification_failed: True` rather than claiming
  clean success on top of an unresolved transaction.

## V3-2: the impossible state removed; exact algorithm order adopted

v3's "successful, verified repair, but primary rerun fails" row was
incoherent — the algorithm's own comparison step already requires rerunning
the primary query to have anything to compare against the reference; there
is no separate rerun that happens *after* verification that could
independently fail. Removed. Adopting Ari's exact sequence as the
authoritative order (not restating it differently):

1. Capture ownership once (`had_txn` at entry). Start an owned snapshot
   (`BEGIN DEFERRED`) or use the caller's already-open one.
2. Read primary result and reference corpus from that snapshot; compare.
3. On mismatch: `SAVEPOINT` → repair (rebuild + purge) → fetch reference →
   **rerun primary once** → compare.
4. Equal → that rerun's result **is** the already-verified result. Release
   savepoint. Finalize per V3-1. Return it.
5. The rerun needed for comparison itself fails (raises), or the repair
   step fails, or the comparison still disagrees → **`ROLLBACK TO
   SAVEPOINT`**, release, finalize per V3-1 (rollback branch), return a
   declared failure — `ok: false`, no `count`/`memories` keys (not an
   empty-but-successful claim) for the rerun-raised case; the existing
   `repair_verification_failed`/`index_repair_failed` flags for the
   comparison-disagrees/repair-SQL-failed cases respectively. All three
   are now explicitly labeled as "repair rolled back," not left implicit.

No extra post-verification rerun exists anywhere in the corrected flow —
removes both the contradiction and the ambiguity about which failure path
applies.

**Snapshot-retry exhaustion, resolved explicitly (Ari's instruction):** on
exhausting all 3 attempts, keep *only* the final attempt's own
primary/reference pair, labeled unverified (`index_verification_failed:
True`) — never resurrect an earlier, discarded attempt's pair. If the
final attempt never produced a primary result at all (failed before that
point), return `ok: false` with no result/count claim, matching step 5's
rerun-failure shape rather than inventing a third response shape for what
is structurally the same "nothing to honestly report" situation.

## V3-3: error classification preserved through to the retry decision

Real implementation blocker, correctly caught: `_ensure_fts_index_consistent`
(as it exists in the reviewed source, `mcp_server.py:692-693`) catches bare
`sqlite3.Error` — which `SQLITE_BUSY_SNAPSHOT` (raised as an
`sqlite3.OperationalError`) is a subclass of — and collapses it to a plain
`False`. An outer wrapper built on that return value literally cannot tell
"ordinary repair failure" apart from "busy-snapshot, restart the whole
attempt," because the information needed to tell them apart is destroyed
before it reaches the wrapper.

**Fix: an internal structured outcome, not a bare bool, used only within
this new verification path** — `_ensure_fts_index_consistent`'s existing
public bool-returning contract is left alone for its other, unrelated
callers (it's called elsewhere, e.g. `_cold_start_check_once`, which has
no need for this distinction and shouldn't have its calling convention
changed for this). Concretely: the verification path calls the rebuild+
purge logic through a thin internal variant (or wraps the same call with
its own `try`/`except sqlite3.OperationalError as e`) that inspects
`e.sqlite_errorcode` (the actual SQLite extended result code, via the
`sqlite3` module's error-code attribute — not string-matching the
exception's message text, per Ari's explicit instruction) and re-raises or
returns a typed marker distinguishing "busy-snapshot, classify for retry"
from "any other `sqlite3.Error`, treat as ordinary repair failure" —
**before** savepoint cleanup runs, so the classification survives the
`ROLLBACK TO SAVEPOINT` / `RELEASE` sequence rather than being lost inside
it.

**"Handle the same condition at any stage that can raise it"** — busy-
snapshot isn't only possible during the repair's own writes; the reference
fetch and the primary rerun (both reads, but reads that can still upgrade
lock state during a `DEFERRED` transaction's first-write-since-read moment)
need the identical classification applied, not just the repair step. Same
`sqlite_errorcode` check wraps all three operations inside the savepoint
block, not only the rebuild/purge call.

## Budget wording, corrected to be honest about what it actually does

v3 described the per-checkpoint time check as if it could "abort" work in
progress. Ari's correction is right: checking elapsed time *before*
starting the next step is cooperative scheduling with bounded granularity —
it can skip a step that hasn't started yet, it cannot interrupt one already
running (a single long SQL statement or Python row-copy loop already in
flight will finish on its own regardless of the checkpoint). Corrected
language: **this is a cooperative budget, not a hard ceiling** — stated
plainly rather than implying capability it doesn't have. If hard
cancellation is actually wanted later, that's a separate, larger mechanism
(SQLite's `set_progress_handler` for interrupting long-running SQL
mid-execution, plus explicit periodic checks inside any Python-side
row-copy loop) — not designed here, not needed to agree on the cooperative
version now, and the actual numeric budget still follows real measurement
per D4, unchanged. **Timeout-triggered abort uses the identical savepoint
rollback path as any other failure** — no special-cased shortcut, no
silent bypass of verification via a timeout.

## Acceptance additions (Ari's three, plus the two from v3 retained)

1. The real repair helper's `SQLITE_BUSY_SNAPSHOT` (via the same
   authorizer/fault-injection technique already used elsewhere in this
   test suite) actually reaches the restart-classification logic — not
   just a hand-constructed exception, the genuine error path.
2. Primary-rerun failure (raised, not just a mismatch) during the
   post-repair comparison rolls the repair savepoint back — confirms V3-2's
   corrected step 5, not the removed impossible state.
3. Final-attempt exhaustion returns only that attempt's own pair (or
   `ok: false` with no result claim if that attempt never got far enough),
   never a resurrected earlier attempt's discarded primary/reference pair.

v3's five acceptance additions and all prior batteries (v1/v2 plus REV7
through REV11-adapted, R9/R10/R11 self-audit tests) remain required,
unchanged.

## Still open, unchanged

Real busy/retry convention elsewhere in the codebase (still not
cross-checked against the 3-attempt bound). D4's actual performance
numbers (still unmeasured; this amendment only sharpens what the
cooperative-checkpoint mechanism can honestly promise). Whether `ORDER BY
rank, m.id` affects other callers (still unchecked). Whether a hard
cancellation mechanism is ever wanted, versus the cooperative budget being
sufficient (not decided, not blocking).
