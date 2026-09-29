# Restore-Time Search Correctness — v5 Consolidated State Machine

Status: DRAFT v5. Written in response to Grok's independent blind review of v2
(SHA256 `7c9b3ef66b93c9a0704b356e26ad4bd6ec1d3298d654ffab6565a6a8c54e05fd`), v3
(SHA256 `ae92fdbc21ab34a4633f2bddb41b07d160c5c0bb475dd734c8b81d5049b500bf`), and v4
(SHA256 `c2144e7a015fd130d6b12dcdcebf856d575fb83c441eb5040bed63790bd0dd5d`) —
verdict **REVISE (narrow)**: the algorithm (direction A, ordered top-k ids,
one bounded repair, snapshot ownership, savepoint-around-repair,
busy-snapshot-not-busy-timeout, cooperative budget) is not reopened and is
not in question. What was not shippable was the *packet as a spec* — three
files that give three different answers for the same outcome (B1), an
unspecified reference-table schema (B2), and an unstated rule for which
result list goes back to the caller after a rolled-back repair (B3).

This document is now the **single authoritative source** for outcome →
response shape. Where it conflicts with anything in v2, v3, or v4, this
document wins and the earlier table/row is superseded, not merely amended.
Direction A, D1's ordered-id algorithm, D3's predicate, D4's scale/entry-point
findings, the 3-attempt snapshot-retry bound, and the savepoint-around-repair
structure are all still settled from v2/v3/v4 and are restated here only
where needed for the table to be self-contained — not reopened.

## Terms, fixed for this document

- **`primary_pre`** — the production FTS query's ordered top-k id list, read
  once at step 2, before any savepoint exists. Held in Python for the rest
  of the call. Never re-read from the DB after this point.
- **`reference`** — an ordered top-k id list built from a `:memory:` FTS5
  table populated from the *full eligible* `memories` corpus (see B2 for the
  schema this table must use). Rebuilt fresh any time this document says
  "fetch reference."
- **`had_txn`** — captured once, at entry, before anything else: whether the
  caller already had an open transaction. If false, this operation owns a
  snapshot it starts itself (`BEGIN DEFERRED`) and is responsible for
  finalizing it per the rules below. If true, this operation never commits
  or rolls back the caller's transaction, in either direction, regardless of
  outcome — only its own savepoint (opened in step 4, if reached) is ever
  resolved.
- **busy-snapshot** — `SQLITE_BUSY_SNAPSHOT`, detected via the `sqlite3`
  module's `sqlite_errorcode` attribute on a caught `sqlite3.OperationalError`
  — never by matching the exception's message text. Classified this way at
  every point in the flow below that touches the database (initial read,
  repair, reference fetch, primary rerun), not only inside the repair call.
  Distinct from an ordinary lock wait (`busy_timeout` territory) and handled
  by restarting the *whole attempt* from step 1, up to 3 total attempts —
  never by retrying only the failed sub-step.

## The flow

1. Capture `had_txn`. Open an owned snapshot if not `had_txn`, else use the
   caller's.
2. Read `primary_pre` and `reference` from this snapshot.
   - If this read raises busy-snapshot → this counts as one exhausted
     attempt; restart from step 1 (see "Snapshot-retry exhaustion" below for
     what happens after 3).
   - If this read raises any other `sqlite3.Error` → **B1 row 0**: no
     savepoint was ever opened, so there is nothing to roll back to. If
     owned, roll back the outer transaction anyway (consistent with "any
     unsuccessful outcome rolls back an owned transaction," even though
     nothing was written). If caller-owned, leave it untouched. Return
     `ok: false`, no `count`/`memories` keys. This is Grok's acceptance-gap
     #3 (owned-txn verification-fetch failure) — it did not have an explicit
     answer before this document; it does now.
3. Compare `primary_pre` to `reference` (ordered, tie-break `ORDER BY rank,
   id` applied identically to both).
   - **Equal** → **B1 row 1**. No repair needed. `ok: true`, no flags,
     return `primary_pre`. Finalize: commit if owned; leave caller's
     transaction alone if not.
   - **Not equal** → proceed to step 4.
4. `SAVEPOINT`. Repair (rebuild + purge). Fetch `reference` again (fresh —
   the corpus may have changed since step 2, or the repair may have fixed
   what step 2 saw). Rerun the primary query once. Compare the rerun to the
   fresh reference.
   - Any of {repair, fresh reference fetch, primary rerun} raises
     busy-snapshot → `ROLLBACK TO SAVEPOINT`, release. This counts as one
     exhausted attempt; restart the whole call from step 1 (see
     "Snapshot-retry exhaustion").
   - Any of the three raises a non-busy-snapshot `sqlite3.Error` →
     `ROLLBACK TO SAVEPOINT`, release. **B1 row 2.** `ok: false`, no
     `count`/`memories` keys. Flag `index_repair_failed` if the repair step
     itself raised; no separate flag if it was the reference-fetch or rerun
     that raised (same "nothing honest to report" shape either way, per v4's
     own instruction to handle the condition identically "at any stage that
     can raise it"). Finalize: roll back owned transaction; leave
     caller-owned alone.
   - All three succeed, but the rerun's ids still don't match the fresh
     reference → `ROLLBACK TO SAVEPOINT`, release. **B1 row 3 (was the v3
     "impossible row"; this is its actual, coherent replacement).** `ok:
     true`, flag `repair_verification_failed`, return **`primary_pre`** —
     the original pre-repair list captured in step 2, never the rolled-back
     rerun's list (**B3**: this is the one sentence B3 asked for). Do not
     re-read primary from the DB to "refresh" after the rollback — that read
     could observe a later writer and silently violate the snapshot
     isolation the whole design depends on. Finalize: commit the outer
     transaction if owned (the repair was rolled back; nothing outstanding
     needs to be rolled back at the outer level — there is no write left
     to discard). This is the "`ok: true` + stale hits + a flag" shape Grok
     confirmed is direction A working as intended, not a bug — callers are
     specified to read the flag, not to infer correctness from `ok` alone.
   - All three succeed and the rerun's ids match the fresh reference →
     `RELEASE` the savepoint (keep the repair). `ok: true`, no error flags,
     return the rerun's id list — it is now the verified result. Finalize:
     commit if owned.

## Snapshot-retry exhaustion

After 3 total attempts (each full restart from step 1) have each ended in a
busy-snapshot classification, stop retrying. Keep **only** the final
attempt's own state — never resurrect an earlier, discarded attempt's
`primary_pre`/`reference` pair:

- If the final attempt got at least as far as completing step 2 (so a
  `primary_pre` from *that* attempt exists in memory, even though the attempt
  never reached a verified outcome) → `ok: true`, flag
  `index_verification_failed: True`, return that final attempt's own
  `primary_pre`. This is an honest, if unverified, result — better than
  discarding a real read.
- If the final attempt never completed step 2 (busy-snapshot hit before any
  `primary_pre` existed) → `ok: false`, no `count`/`memories` keys — the same
  shape as B1 row 0/row 2, for the same reason: nothing honest to report.

## Finalization can itself fail

`COMMIT`/`ROLLBACK` are real SQL statements and can themselves raise. This is
checked *after* every branch above decides what it would return, before
anything is actually returned: if the transaction this operation owns fails
to finalize (in either direction), the comparison result — however clean —
is not trustworthy on its own, because this operation's own bookkeeping
didn't complete. **Never return a verified-success (or verified-stale, i.e.
`repair_verification_failed`) result on top of a failed finalization.**
Downgrade to `ok: false`, `index_verification_failed: True` instead,
regardless of which branch above was about to be returned. Caller-owned
transactions are never finalized here in the first place, so this check only
applies to the owned case.

## B1 — the one table, stated plainly

| Outcome | `ok` | Flags | Returned ids | Savepoint | Outer txn (if owned) |
|---|---|---|---|---|---|
| Verification-fetch fails before any comparison (step 2) | false | — | none | never opened | rollback |
| No mismatch at step 2 | true | — | `primary_pre` | never opened | commit |
| Repair/reference-fetch/rerun raises non-busy sqlite3.Error | false | `index_repair_failed` (repair step only) | none | rollback to savepoint | rollback |
| Repair succeeds, rerun still disagrees with fresh reference | true | `repair_verification_failed` | `primary_pre` (never the rerun) | rollback to savepoint | commit |
| Repair succeeds, rerun matches fresh reference | true | — | rerun's ids (== reference) | release | commit |
| Busy-snapshot at any stage, attempts remain | *(retry, not a terminal outcome — restart from step 1)* | | | | |
| Busy-snapshot exhausted, final attempt had a `primary_pre` | true | `index_verification_failed` | final attempt's own `primary_pre` | rollback to savepoint (if one was open) | rollback |
| Busy-snapshot exhausted, final attempt had no `primary_pre` | false | — | none | n/a | rollback |
| Owned finalization itself fails | false | `index_verification_failed` | none | n/a — result overridden regardless of branch above | already attempted; failure is the report |
| Caller-owned transaction, any outcome above | *(same `ok`/flags/ids as the matching row)* | | | *(only the savepoint, if any, is ever resolved)* | **never touched, either direction** |

v4's V3-1/V3-2 are superseded by this table, not merely restated — in
particular, this table makes explicit what v4 left implicit: the
verification-fetch-failure row (B1 row 0, new), and that
`repair_verification_failed` finalizes as a **commit** (there is no
outstanding write to discard once the repair savepoint is rolled back — v3's
row that tried to make this case roll back the *outer* transaction was
conflating "the repair didn't stick" with "the whole read failed," which are
different things). v2's D3 table (all outcomes `ok: true`) is superseded
outright; it predates the `index_repair_failed`/verification-fetch-failure
distinction entirely.

## B2 — reference table schema, specified

The `:memory:` FTS5 table used to build `reference` **must** be created with
the identical `tokenize` argument, column list, and prefix-index options as
production `memories_fts` (`content=memories, content_rowid=id`, whatever
tokenizer — porter/unicode61 — and prefix settings production actually uses;
read them off the live schema at repair-helper-authoring time rather than
hand-typing a guess here, and keep the two in one place so they can't drift
apart silently). If the in-memory table uses SQLite's FTS5 defaults instead
of matching production, `bm25` ranking order will not match even on a
provably healthy database, and every single search would trigger the repair
path — silently defeating the entire "healthy DB, no file replace →
verification is a no-op" acceptance case.

**New acceptance case (closes the gap Grok named):** healthy corpus, in-memory
reference table built with the same schema/tokenizer as production → zero
repairs triggered, no flags set, across a batch of real queries, not just one.
This is what actually makes the existing "healthy limits" case meaningful —
without schema parity confirmed, that case could pass by accident.

## B3 — restated as the one sentence it needed

After `ROLLBACK TO SAVEPOINT`, the response's id list is **`primary_pre`** —
captured once, in step 2, before the savepoint ever opened — never the
rerun's list (which describes a repair that was just undone) and never a
fresh re-read of primary from the database (which could observe a write that
happened after this operation's snapshot was taken, silently breaking
isolation). This is now stated once, in the B1 table, rather than left to be
inferred per-row.

## Acceptance additions (Grok's four, on top of everything in v3/v4)

1. Healthy corpus, schema-matched reference table: zero repairs, no flags,
   across multiple real queries — not the same single query repeated (closes
   B2).
2. After a `repair_verification_failed` outcome: confirm directly (query the
   DB, not just trust the return value) that the DB's actual FTS rows equal
   their pre-repair state — the rollback really happened — **and** that the
   response's id list is `primary_pre`, not the rolled-back rerun's list.
3. Verification-fetch failure at step 2, owned transaction: confirm the
   outer transaction is actually rolled back (not left open), `ok: false`,
   no `count`/`memories` keys.
4. `SQLITE_BUSY_SNAPSHOT` injected during the post-repair **read** stage
   (reference fetch or primary rerun), not only during the repair/rebuild
   call itself — v4 already asked for this; carrying it forward explicitly
   so it isn't lost in the v5 consolidation.

All acceptance items from v2, v3, and v4 (D1-D4, V2-1, the 3-attempt bound,
REV7 through REV11-adapted, R9/R10/R11 self-audit tests) remain required,
unchanged by this document.

## Still open, unchanged from v4

Real busy/retry convention elsewhere in the codebase, not yet cross-checked
against the 3-attempt bound here. D4's actual performance numbers, still
unmeasured. Whether `ORDER BY rank, m.id` affects other callers of the
production query, still unchecked. Whether a hard-cancellation mechanism is
ever wanted on top of the cooperative budget — not decided, not blocking.

## Not reopened

Direction A itself. D1's ordered-top-k-id comparison. D3's predicate for
which corpus is "eligible." D4's scale/entry-point findings. The 3-attempt
snapshot-retry bound as a number. The savepoint-around-repair-and-reverify
structure. Busy-snapshot classified via `sqlite_errorcode`, never message-text
matching. The cooperative (not hard-abort) nature of the time budget. Grok's
review confirmed all of these hold; this document exists only to make B1-B3
unambiguous on top of them.
