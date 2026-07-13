"""Regression coverage for the safety tranche authorized on THE-65 (2026-07-13).

Cluster 1: the `dry_run` NameError in cmd_consolidation_cycle's non-phased path.

ROOT CAUSE: `cmd_consolidation_cycle` (hippocampus.py) has two code paths --
a `--phased` path (calls `run_phased_consolidation(db, dry_run=getattr(args,
'dry_run', False))`, which correctly derives `dry_run` from `args`) and the
default non-phased path, which never defines a local `dry_run` variable at
all. Pass 7b (procedural synthesis) references `dry_run=dry_run` anyway.
Because that call is wrapped in a bare `try/except Exception`, the resulting
`NameError` does not crash the cycle -- it gets caught and silently turned
into `procedural_stats = {"error": "name 'dry_run' is not defined", ...}`.
The overall cycle then commits, logs a `consolidation_cycle` event, and
reports success, with the error text buried inside the event's JSON detail
where nothing surfaces it. This is the same "exception swallowed into a
success-shaped result" pattern found independently three other places in
this codebase during the 2026-07-12/13 audit (see THE-65 for the other four
instances) -- procedural synthesis has been silently broken on every real
non-phased consolidation-cycle run since this code was written.

Before the fix: this test is EXPECTED TO FAIL, because the CLI output's
`procedural_synthesis` field contains the swallowed NameError instead of
real stats. That failure IS the reproduction.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"


def run_consolidation_cycle(db_path, agent="tester"):
    """Run `brainctl consolidation-cycle` via subprocess against db_path.

    Uses BRAIN_DB (not the _impl.DB_PATH monkeypatch other CLI tests use)
    because hippocampus.get_db() resolves its database path from the
    BRAINCTL_DB/BRAIN_DB environment variables (see agentmemory/paths.py),
    not from _impl.DB_PATH -- that patch only affects _impl-routed commands.
    """
    env = {**os.environ, "PYTHONPATH": str(SRC), "BRAIN_DB": str(db_path)}
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r); "
         "from agentmemory.hippocampus import main; "
         "sys.argv = ['brainctl-consolidate', 'consolidation-cycle', '--agent', %r]; "
         "main()" % (str(SRC), agent)],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert result.returncode == 0, (
        f"consolidation-cycle failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout[:1000]}\nstderr: {result.stderr[:1000]}"
    )
    return json.loads(result.stdout)


def test_procedural_synthesis_does_not_silently_swallow_dry_run_nameerror(cli_db):
    """The actual regression test for the dry_run bug found while verifying
    GPT's independent claim on THE-65 (2026-07-13).

    Runs the real, default (non-phased) consolidation-cycle CLI path against
    a fresh, empty, full-schema database and inspects the procedural_synthesis
    field of the returned summary directly.

    Pre-fix: `procedural_synthesis` is `{"error": "name 'dry_run' is not
    defined", "candidates_updated": 0, "promoted": 0}` -- the NameError,
    caught and disguised as an ordinary result.

    Post-fix: `procedural_synthesis` has no `error` key at all (there's
    nothing procedural to synthesize on an empty DB, so `candidates_updated`
    and `promoted` are both 0 -- but for the *real* reason, not a swallowed
    exception).
    """
    data = run_consolidation_cycle(cli_db)

    procedural = data.get("procedural_synthesis")
    assert procedural is not None, "consolidation-cycle summary is missing procedural_synthesis entirely"

    # THE ACTUAL ASSERTION UNDER TEST: no error key, and specifically not the
    # swallowed NameError. Everything above this line is just setup and the
    # real subprocess run; this line is the reproduction/regression check.
    assert "error" not in procedural, (
        f"procedural synthesis silently failed instead of running: {procedural!r}"
    )
