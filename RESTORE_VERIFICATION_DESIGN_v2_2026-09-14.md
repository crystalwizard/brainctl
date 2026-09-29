# Restore-Time Search Correctness — Design Document, v2

Status: DRAFT v2, revising v1 per Ari's REV1 design review (HOLD, REVISE
before implementation, verdict supports direction A). v1's hash and Ari's
full review are on BCTL-1. This version is itself being published as an
immutable, versioned, hashed file per his explicit process request — no
more mutable in-place edits to a design under review.

v1 covered the corrected problem statement and the corpus-size/restore-path
factual questions; both held up (measured corpus is genuinely small; no
restore-from-backup path exists). This version answers his four concrete
design gaps (D1-D4) and the acceptance packet, since v1 stopped at "build a
reference table" without specifying the actual algorithm.

## D4 first, because it changes the frame of D1-D3: precise restore-path inventory

Ari correctly flagged "no restore/import/backup entry point exists" as too
broad. Checked precisely, not re-asserted:

- **`cmd_backup`** (`_impl.py:9068`): writes only. Copies the live `.db` file
  and a `.sql` dump into `BACKUPS_DIR`, prunes old ones. Grepped every use of
  `BACKUPS_DIR` in the tree: it is written to and globbed for pruning, never
  read back into the live database. **No command anywhere restores a backup
  into place.**
- **`cmd_import`** (`commands/import_cmd.py:46`): onboarding from *other*
  memory providers (mem0, JSON). Inserts row-by-row through the canonical
  `cmd_memory_add` path — real Python-level writes, which issue real SQL
  `INSERT`s against `memories`, which the `memories_fts_insert AFTER INSERT`
  trigger fires on same as any other insert. **Not an instance of this
  vulnerability class** — the trigger keeps FTS in sync because this goes
  through a live INSERT, not a file-level swap.
- **`cmd_merge`** (`_impl.py:18481`, `merge.py:545`): merges a source
  `brain.db` into a target `brain.db` via `ATTACH DATABASE` plus real SQL
  `INSERT OR IGNORE INTO memories (...) VALUES (...)` statements against the
  target connection. Same reasoning as import: this is a live INSERT on the
  target connection, the AFTER INSERT trigger fires, FTS stays in sync.
  **Also not an instance of this vulnerability class.**

**The actual, only path into the vulnerability every REV7-REV12 fixture has
tested:** an out-of-band, manual file-level replace of `brain.db` (a raw
`cp`/`shutil.copy2` of some prior `.db` snapshot over the live file, done
outside any brainctl command) — which transplants FTS shadow-table content
verbatim, decoupled from whatever the `memories` table says at the moment
the file lands. This is real (`cmd_backup` produces exactly the kind of
`.db` file someone could later manually copy back), but it is genuinely
**not a code path in this repository at all** — it happens entirely outside
the process, by definition, which is why passive detection (this whole line
of fixes) is the only lever available today, and why "build a restore
command and require it to invalidate" (option B) would be inventing new
governed behavior, not wiring an existing gap shut.

This sharpens the design: there is no in-process moment to hook an
invalidation call into. Verification has to happen at *read* time, because
there is no corresponding *write* event to hook at all.

## D1: The algorithm, specified exactly

**What gets compared:** ordered top-k real IDs, not scores, not raw
postings. Rationale: comparing floating bm25 scores directly is fragile
(float equality, and SQLite doesn't guarantee bit-identical scores across
separately-constructed FTS5 tables even over identical content, per
observed behavior in the R10/R11 self-audit tests). Comparing ordered ID
sequences is what the caller actually consumes and what "the right answer"
means operationally.

**Reference corpus:** every row where `indexed = 1 AND retired_at IS NULL`,
across all agents — the same population the real `memories_fts` index's
`content=memories, content_rowid=id` external-content configuration draws
from, so bm25's table-wide document count and average length match the
real index (this is the point Ari confirmed via the SQLite docs: bm25 uses
table-wide statistics, not the caller's query scope).

**Caller scope applied where, exactly:** to *result selection*, not to the
population used for ranking. Concretely: build the reference table from the
full eligible population (as above); run the query's actual `MATCH` +
caller `WHERE` conditions (borrow_from/category/scope/memory_type — same
conditions already computed in `tool_memory_search`) against the reference
table's *rowid* set via a second, cheap filter step (an `IN (...)` restricted
to eligible ids under the caller's own scope, computed identically to the
real query's own `_fetch_eligible_content` scoping) — ranked using the
full-corpus statistics, filtered to the caller's actual visibility. This is
the concrete mechanism behind v1's underspecified "apply scope to selection,
not ranking" instruction.

**Tie-breaking, explicit:** SQLite FTS5's `rank` alone is not guaranteed
total-order-stable across ties. Both the real production query and the
reference query must apply the *same* deterministic secondary sort —
`ORDER BY rank, id` (ascending id as the tie-break) — for the comparison to
be meaningful. This requires changing the real production query's own
`ORDER BY rank` (currently in `tool_memory_search`'s `_run_primary_query`)
to `ORDER BY rank, m.id`, not just the reference query — an actual, small
production behavior change, called out explicitly rather than left
implicit.

**Comparison and repair:**
1. Run reference query, get `reference_top_k` (ordered ids, tie-broken).
2. Compare to `returned_ids_in_order` (from the real primary query, same
   tie-break applied).
3. Equal → verified, return unchanged, no repair attempted.
4. Not equal → one bounded repair attempt (`_ensure_fts_index_consistent`,
   unchanged from R9-B1/R9-B2), then **re-run and re-verify against a fresh
   reference query** — do not assume a successful `INSERT ... rebuild` SQL
   statement proves the *result* is now correct; confirm it the same way
   the initial mismatch was detected. This directly answers Ari's "verify
   the repair itself" requirement and his warning against assuming SQL
   success implies data correctness.
5. Post-repair mismatch persists → return the post-repair result with an
   explicit `repair_verification_failed: True` flag (distinct from
   `repair_failed`, which means the rebuild SQL itself errored — this means
   the rebuild ran without error but didn't actually fix the disagreement,
   a real, distinct failure mode worth its own signal). No second repair
   attempt — bounded to one, per Ari's explicit instruction.

**Scope of the guarantee, stated plainly (Ari's D1 question):** this covers
*primary FTS retrieval only*. `tool_memory_search` applies rerankers and a
separate multi-pass query after this helper runs (confirmed by reading the
surrounding code, not assumed) — those later stages are NOT covered by this
verification and can still reorder or add results this mechanism never
checked. This needs to be stated in the code's own docstring and in any
caller-facing documentation, not left implicit the way v1 left it.

## D2: Snapshot and transaction ownership, specified exactly

**Consistent snapshot:** the reference query and the primary query must
read the *same* database state, or a legitimate concurrent write looks like
corruption. Concrete mechanism: run both the primary query and the
reference-corpus fetch inside a single `BEGIN DEFERRED` read transaction on
the *real* connection (`db`, the caller's connection) before either query
runs — SQLite's isolation model guarantees a `DEFERRED` transaction sees a
consistent snapshot for its lifetime once the first read happens, so
fetching eligible content and re-running the primary query both see the
same data even if another connection commits a write in between. The
disposable `:memory:` reference table is built FROM that already-consistent
snapshot's fetched rows, not from a second separate read against the real
file — so its content is guaranteed consistent with what the primary query
saw, by construction, not by hoping nothing changed between two reads.

**Caller transaction ownership (same R2-F1/R10-transaction-finding pattern,
applied here too):** if the caller already has an open transaction
(`had_txn = getattr(conn, "in_transaction", False)`, checked *before* this
verification's own `BEGIN DEFERRED`), this function must not start a nested
transaction that could interact badly with the caller's own — SQLite does
not support true nested transactions; use `_commit_if_owned`'s existing
had-txn-tracking pattern (only commit/rollback if this call is the one that
opened the transaction) rather than inventing a new mechanism.

**Busy/snapshot conflicts:** a bounded retry (matching whatever retry
policy the rest of this codebase already uses for `sqlite3.OperationalError:
database is locked` — needs checking against existing conventions rather
than inventing a new one) rather than an indefinite wait or an immediate
hard failure.

**Reference connection disposal on all exits:** already fixed for the
existing `_fts_matches_real_content` in the R10-B3 fix (`finally: if
verify_conn is not None: verify_conn.close()`) — carry that same pattern
into whatever function replaces/extends it here, including on the new
repair-verification-retry path.

**Linearization point, stated honestly:** this guarantee covers "correct as
of the snapshot this call's transaction observed." It does not and cannot
promise correctness against a file being replaced *during* this call
(SQLite's os-level file handle semantics on a replaced file are themselves
undefined territory this design doesn't attempt to solve) — worth one
explicit sentence in the docstring rather than an implied, unbounded promise.

## D3: The degradation predicate, corrected

Ari is right that v1's `len(real_ids) > expected_count` fires on *every*
healthy truncated query, not just the R12 omission case — it's not a
degradation signal at all as written, it's just "was this query truncated,"
true or false, unrelated to whether truncation was handled correctly.

With the D1 algorithm actually in place (ordered top-k comparison against a
correct reference), the distinct outcomes are:

| State | `ok` | New/existing flag | Results returned |
|---|---|---|---|
| Verified (reference matches returned, no repair needed) | `true` | none | as-is |
| Repaired and reverified (mismatch found, rebuild ran, re-verify confirms fix) | `true` | none (same as today's clean-repair case) | freshly re-run, verified |
| Reference-fetch or in-memory FTS check itself failed to run | `true` | `index_verification_failed` (existing, reused per Ari's instruction — not a new overlapping flag) | as originally returned, unverified |
| Rebuild SQL itself errored | `true` | `index_repair_failed` (existing) | as originally returned (still stale) |
| Rebuild ran without error but re-verification still disagrees | `true` | `repair_verification_failed` (new — distinct failure mode, see D1 step 5) | post-repair result, still potentially wrong |

No flag at all for ordinary truncation that verifies correctly — that's the
normal, expected, healthy case, and v1's mistake was treating "truncated"
as inherently suspicious rather than "truncated AND unverifiable/wrong" as
the actual signal.

## D4: Performance — hypothesis, not a claim, plus a stated budget

Corrected framing per Ari's instruction: 213 total / 212 eligible / 6
agents / 4MB is real, author-measured *scale* evidence (read-only, direct
sqlite3 access against the live file, not independently reproduced by
Ari) — not a timing or memory benchmark, and v1 was wrong to call the
resulting cost "trivially cheap" without measuring it.

**Actual hypothesis to test, on synthetic disposable corpora, before
claiming this is viable:** rebuilding a full in-memory FTS5 table from the
complete eligible population, on every verified search call, at realistic
content lengths (not the short test-fixture strings used in the adversarial
packets) and at a control size well above the current real 212 rows (an
order of magnitude up, to see how the cost curve actually moves, not just
its value at today's size). Needs real measurement before implementation,
not before this design is agreed — but the design should specify the
budget now: **a stated wall-clock ceiling per verification call** (a
number, e.g. picked after the synthetic measurement, not invented here),
and **honest, visible behavior if that ceiling is exceeded** — the same
`index_verification_failed` signal, not a silent skip. The design must not
let a slow verification quietly degrade into "no verification happened,"
unflagged.

## Acceptance test battery (Ari's list, restated as the actual plan)

- R12 missing-best-hit (the limit=1, stronger-match-stale case)
- R11 stale relevance order (non-truncated, same ID set, wrong order)
- Healthy limits and tier caps with **no** repair triggered (the R11-B1
  regression control, still required to stay green)
- **Tied ranks at the cutoff** — two real matches with identical rank,
  right at the limit boundary; both the real query and reference must
  apply the same `id` tie-break for this to be meaningful, not flaky
- Category-only and tags-only matches, both directions (R10-B2's existing
  coverage, must still hold under the new algorithm)
- **Scoped queries whose ranking depends on other agents' corpus** — a case
  v1 didn't test at all: a `borrow_from`/category-scoped query where the
  *visible* result set is small but bm25 statistics are computed over the
  full multi-agent corpus; confirm the reference construction (full corpus,
  scope applied at selection) actually produces the right ranking here,
  not just in the single-agent case every existing fixture uses
- `indexed = 0` / `retired_at IS NOT NULL` exclusion, still correctly
  excluded from both the real query and the reference population
- **Stale extra postings** — not just missing real matches, but a stale
  posting for content that should NOT match anymore (e.g. edited away from
  matching), confirming the reference correctly excludes it and the
  comparison catches an over-broad stale result, not just an under-broad one
- Verification-fetch failure and in-memory-FTS-check failure (existing
  R10-B3 coverage, confirm it still holds through the new algorithm)
- Repair SQL failure (existing R9-B2 coverage)
- **Repair-ran-but-verification-still-disagrees** (new case from D1 step 5,
  not covered by any existing test)
- Caller transaction preservation (existing R10/R11 coverage, extend to
  cover the new `BEGIN DEFERRED` snapshot mechanism specifically)
- A concurrent-change case: a real write commits between the primary query
  and the reference fetch, confirm the snapshot mechanism (D2) actually
  prevents that from reading as corruption
- Confirm reranker/multi-pass stages (if any run in the same call) are
  either also covered or the result honestly states they aren't (D1's
  scope-of-guarantee point)

Existing passing regression packets (REV7 through REV11-adapted, R9/R10/R11
self-audit tests) must all still pass unchanged.

## What's still genuinely unresolved in this v2

- The exact busy/retry policy for D2 hasn't been checked against existing
  codebase convention yet — needs a quick grep before implementation, not
  invented fresh.
- D4's actual performance numbers don't exist yet — this design specifies
  what to measure and how to fail honestly if the budget is exceeded, not
  the numbers themselves.
- Whether `ORDER BY rank, m.id` (the D1 tie-break change) has any
  observable effect on existing callers/tests beyond this fix needs
  checking before it ships as a production query change.

Published as an immutable file, this exact version. SHA256 to be computed
and posted alongside this on BCTL-1 once written to disk.
