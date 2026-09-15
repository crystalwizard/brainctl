# Restore-Time Search Correctness — v3 Amendment (closes V2-1, V2-2)

Status: DRAFT v3, amendment to v2 (SHA256
`7c9b3ef66b93c9a0704b356e26ad4bd6ec1d3298d654ffab6565a6a8c54e05fd`), not a
replacement. Per Ari's REV2 design review: direction A, D1's algorithm, D3's
corrected predicate, and D4's scale/entry-point findings are accepted and
not reopened here. This amendment closes exactly the two remaining gaps he
named (V2-1: stale-snapshot recovery, V2-2: atomic repair cleanup) and
corrects three points of overclaimed language he caught in v2's prose.

## V2-1: stale-snapshot recovery, specified precisely

`busy_timeout` (already set by `get_db()`) handles ordinary lock
contention — waiting for another connection to release a lock. It does
**not** address a `DEFERRED` transaction's read snapshot going stale: per
SQLite's own isolation documentation, a read-to-write upgrade after another
writer has committed can fail with `SQLITE_BUSY_SNAPSHOT`, and the fix is
not "wait and retry the same statement" — the existing snapshot has to be
abandoned and a new one started. Retrying the identical write inside the
same stale snapshot cannot succeed.

**Two ownership branches, both bounded:**

- **This operation owns the transaction** (it called `BEGIN DEFERRED`
  itself, because `had_txn` was `False` on entry): on `SQLITE_BUSY_SNAPSHOT`,
  `ROLLBACK` the whole transaction, discard the primary-query result and
  the reference-corpus fetch together (they're a pair — keeping one and
  refetching the other reintroduces exactly the inconsistent-snapshot
  problem D2 exists to prevent), and restart the entire comparison from a
  fresh `BEGIN DEFERRED`. Bounded to **3 total attempts** (1 initial + 2
  restarts) — chosen as a small, finite number for a contention condition
  that should be rare and self-resolving, not because 3 is derived from
  any measurement; revisit if implementation testing shows it's wrong.
  Exhausting all 3 returns `index_verification_failed: True` (this *is*
  "exhausted snapshot retries" from the state-table gap below, not a
  separate case).
- **The caller already owns the transaction** (`had_txn` was `True` on
  entry): this operation must never roll back a transaction it doesn't
  own — that would discard whatever the caller had pending, unrelated to
  this search. On `SQLITE_BUSY_SNAPSHOT` here, immediately return a
  declared degraded outcome (`index_verification_failed: True`) and leave
  the caller's transaction exactly as it was, fully in their control. No
  retry in this branch — retrying without a fresh snapshot, which only
  this operation can create by rolling back a transaction it owns, cannot
  make progress, per Ari's point.

**Transaction lifecycle, named precisely (the actual gap Ari flagged —
"merely reusing had_txn does not specify the outer owner's lifecycle"):**

- `had_txn` is read once, at entry, before this operation does anything.
- If `had_txn` is `False`: this operation issues `BEGIN DEFERRED` itself
  immediately, runs the primary query + reference fetch + comparison (+
  the savepoint-scoped repair from V2-2, if needed) entirely inside it, and
  **always** ends that transaction itself before returning — `COMMIT` on a
  verified-clean or successfully-repaired-and-reverified outcome, `ROLLBACK`
  on any unrecoverable outcome (verification-failed, repair-failed,
  repair-ran-but-still-wrong, retries exhausted). No path returns to the
  caller with this operation's own transaction still open — that's the
  actual invariant "which component owns those actions" needed: whoever
  opens it here is also unconditionally the one who closes it here, every
  exit path, not just the success path.
- If `had_txn` is `True`: this operation issues no `BEGIN`/`COMMIT`/
  `ROLLBACK` of its own at all (matching `_commit_if_owned`'s existing
  pattern, extended to cover rollback too, which the current code doesn't
  need because it never rolls back). Any repair savepoint (V2-2) is scoped
  entirely within the caller's existing transaction and is released or
  rolled back to, never committed or rolled back at the transaction level —
  that decision stays the caller's, made whenever *they* commit or roll
  back their own work.

**Later recall/access-log writes, addressed:** `tool_memory_search`'s
recall-boost loop and its own `db.commit()` run *after*
`_verify_and_repair_stale_matches` returns, on the same connection, using
whatever transaction state is ambient at that point. Given the invariant
above — this operation never returns with its own transaction left open —
those later writes either land inside a transaction the *caller* already
owned before this function ran (unaffected by anything this function did),
or land in a fresh implicit/auto-commit context after this function's own
transaction has already been cleanly closed. No new coordination needed
between the two *as long as the invariant holds*; the invariant itself is
the thing that makes this true, so it must actually be enforced (tested,
see acceptance additions below), not just asserted.

## V2-2: atomic repair cleanup, specified precisely

`_ensure_fts_index_consistent` currently does rebuild, then
`_purge_ineligible_fts_rows`, catching `sqlite3.Error` around both and
returning `False` on failure — but a failure in the *purge* step, after the
*rebuild* step already succeeded, leaves the rebuild's changes sitting
uncommitted (in whatever transaction is ambient), not rolled back. Under
this amendment's outer-transaction design, an unrelated caller-owned commit
later in the same transaction would sweep that partial repair up and
persist it — real, not hypothetical, exactly the class of bug this entire
line of fixes exists to prevent, reintroduced at a new layer.

**Mechanism: a `SAVEPOINT` scoped around the repair attempt AND its
reverification, not just the rebuild+purge:**

```
SAVEPOINT fts_repair
  -- rebuild
  -- purge
  -- (if both succeed) re-fetch reference, re-run primary query, re-compare
  -- if comparison still disagrees: this counts as repair failure too
ROLLBACK TO SAVEPOINT fts_repair  -- on ANY failure in the block above
RELEASE SAVEPOINT fts_repair      -- always, whether rolled back or not
```

A rollback-to-savepoint followed by release is the correct SQLite sequence
for "undo only this block's changes, then stop tracking the savepoint" —
it does not touch anything the outer transaction owner had pending before
`SAVEPOINT fts_repair` was issued, whether that owner is this operation
(inside its own `BEGIN DEFERRED`) or the caller (inside theirs). This
directly satisfies "preserve unrelated caller changes" and "on success,
release the savepoint; commit only if this operation owns the outer
transaction" — the savepoint itself never commits anything at the
transaction level; only the outer owner's eventual `COMMIT` does, per the
V2-1 lifecycle above.

`_ensure_fts_index_consistent`'s own internal `_commit_if_owned` call
becomes provably inert under this design (it checks `conn.in_transaction`
at its own entry, which will always be `True` here — either the caller's
transaction or this operation's own `BEGIN DEFERRED` is active — so its
existing had-txn guard already prevents it from committing anything; no
code change needed there, just confirming the existing guard composes
correctly rather than assuming it).

**No alternative-to-savepoints policy is proposed** — savepoints are
SQLite's native mechanism for exactly this (nested atomic units inside a
transaction) and I don't see a case for avoiding them here; flagging that
I considered "manual before/after snapshot comparison + explicit undo" as
an alternative and rejected it as strictly more complex for no benefit,
rather than silently picking savepoints without saying why.

**Completed state table** (extends v2's D3 table with the failure points
Ari named — post-repair reference-fetch failure, primary-rerun failure,
retries exhausted):

| State | `ok` | Flags | Results | Transaction outcome |
|---|---|---|---|---|
| Verified, no repair needed | `true` | none | as-is | commit (if owned) |
| Repaired, reverified, confirmed fixed | `true` | none | freshly re-run | commit (if owned) |
| Reference-fetch or FTS-check itself failed (before any repair attempted) | `true` | `index_verification_failed` | as originally returned | commit (if owned) — nothing was changed |
| Rebuild/purge SQL itself errored | `true` | `index_repair_failed` | as originally returned | savepoint rolled back; outer commit (if owned) — original data untouched |
| Repair ran clean but reverification still disagrees | `true` | `repair_verification_failed` | as originally returned (pre-repair) | savepoint rolled back; outer commit (if owned) |
| **Post-repair reference-fetch fails** (new) | `true` | `index_verification_failed` **and** `index_repair_failed` unset — repair itself is not known to have failed, only its *confirmation* couldn't run | as originally returned | savepoint rolled back (can't confirm repair, don't keep unconfirmed state); outer commit (if owned) |
| **Primary-query rerun fails after a successful, verified repair** (new) | `false` | `index_repair_failed: False`, a new field `primary_rerun_failed: True` | none — **no unqualified empty success**; `count`/`memories` keys omitted rather than defaulted to empty, so a caller can't mistake this for a real zero-result search | commit the repair itself (it's genuinely fixed and confirmed — only the *rerun* failed, a different, later step); this is the one case where the repair's own success should still land even though the overall call reports `ok: false` |
| **Snapshot retries exhausted** (V2-1) | `true` | `index_verification_failed` | as originally returned | operation-owned: final rollback of that attempt's transaction; caller-owned: caller's transaction untouched (never entered a retry loop in this branch at all) |

The one asymmetric case above (primary-rerun failure) is deliberate: the
repair itself succeeded and was confirmed correct against the reference —
rolling that back just to keep the state table uniform would throw away a
real, verified fix over an unrelated failure in a later step. Flagging this
as a real judgment call worth a second look, not asserting it's obviously
right.

## Three language corrections (no architecture change, per Ari's own framing)

1. **The `indexed=1 AND retired_at IS NULL` reference population is *our*
   trigger/purge policy, not an SQLite-enforced guarantee.** v2 implied the
   predicate was somehow inherent to FTS5's `content=memories` external-
   content configuration. It isn't — it's this codebase's own choice about
   which rows the triggers index and which the purge step removes. A
   different defect class could in principle produce a raw index that
   disagrees with even that predicate. Noted as a real, distinct residual
   risk, not folded into "restore" language.

2. **"Only path" language in v2 was too strong, corrected:** an ordinary,
   internally-coherent file copy of a healthy database does not itself
   create a memories/postings disagreement — coherent data stays coherent
   across a copy. The disagreement every REV7-REV12 fixture reproduces
   comes from deliberately constructing an *already-inconsistent* database
   and testing against that. File replacement is **a demonstrated way**
   the cache-bypass scenario (stale "already checked" bookkeeping pointing
   at genuinely different content) gets triggered — it is not proof that
   no other defect (a crash mid-write, a trigger silently failing, some
   other bug entirely) could produce the identical disagreement. v2's "the
   only real path in" is corrected to "the path every current fixture
   demonstrates" — narrower, honest, and doesn't imply the fix only needs
   to defend against file replacement specifically.

3. **Performance gate, enforcement specified, not just measured after the
   fact:** a wall-clock budget is only meaningful if it can actually be
   enforced mid-operation, not merely noticed as exceeded once the work is
   already done. Concretely: check elapsed time against the budget at each
   natural checkpoint in the verification (before starting the reference
   fetch, before starting the FTS comparison, before starting a repair
   attempt) and abort — returning `index_verification_failed: True` — at
   the first checkpoint where the budget is already exceeded, rather than
   letting an in-progress query run to completion regardless of cost. The
   actual numeric budget still isn't set here (per v2, real measurement
   comes first) — this specifies the enforcement *mechanism* the budget
   will plug into, so measurement and enforcement aren't designed
   separately and then discovered incompatible.

## Acceptance additions (extends v2's battery, doesn't replace it)

1. A second connection commits a write after this operation's comparison
   snapshot has started; the stale reader then attempts a repair — confirm
   the operation-owned branch restarts cleanly from a fresh snapshot, and
   the caller-owned branch returns a degraded outcome without touching the
   caller's transaction. Both ownership variants required.
2. Rebuild succeeds, purge is then denied (authorizer, matching existing
   fault-injection technique), *then* the caller commits unrelated
   pending work in the same transaction — confirm no part of the partial
   repair persists (the actual V2-2 regression this design exists to
   prevent).
3. Reference re-fetch fails specifically *after* a successful repair
   (timing distinct from existing pre-repair fetch-failure coverage).
4. `rerun()` itself raises after a successful, verified repair — confirm
   the asymmetric state-table row above (repair persists, overall call
   reports `ok: false`, no unqualified empty success).
5. Snapshot-retry exhaustion, operation-owned branch only (caller-owned
   never enters the retry loop, covered by case 1's second variant).

Existing v1/v2-accepted acceptance items and all prior regression packets
(REV7 through REV11-adapted, R9/R10/R11 self-audit tests) remain required
and unchanged.

## Still open, unchanged from v2, not addressed here

Real busy/retry convention elsewhere in this codebase (still not checked —
this amendment specifies `SQLITE_BUSY_SNAPSHOT` handling specifically per
Ari's citation, which is a different condition than ordinary lock
contention `busy_timeout` already covers, but hasn't cross-checked whether
some other existing convention should inform the 3-attempt bound chosen
here). D4's real performance numbers — still unmeasured, this amendment
only specifies how a future budget gets enforced. Whether `ORDER BY rank,
m.id` affects other callers — still unchecked.
