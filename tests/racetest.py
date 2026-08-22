"""Two workers, one run.

Fencing is the third of the three guarantees, and the only one that needs a
second process to demonstrate. The other two are visible in a single worker's
transcript across a restart; this one is not visible at all until somebody else
holds the run.

Two cases:

    claim    both workers start together and fight over one run
    steal    one claims, journals a decision, and stalls. The other takes the run
             and finishes it. The first wakes up holding an epoch nobody honours
             any more, and finds out on its next write.

The first case cannot assert where the loser dies, only that one dies. Two
processes started together rarely reach the compare-and-swap together: whichever
gets there second usually reads the newer epoch and takes the run legitimately,
and the first learns it lost on its next write instead of on the claim. Both
orderings are correct, and a test that insisted on one would fail on the timing
of process startup rather than on anything about the runtime.

The second case is the one worth having, and the reason STALL_AT exists. It
reaches a worker that already holds a run and has journaled a decision, which the
first case reaches only by luck, and it reaches it at a named step every time.

Both mocks must stay up for the whole run, as for crashtest.py.

    python racetest.py           both cases
    python racetest.py steal     one case
"""

import argparse
import asyncio
import subprocess
import sys
import time

import asyncpg

from harness import check, child_env, count, find, mock_calls, remote_keys, snapshot, truncate

from agent_runtime import config
from agent_runtime.logs import banner, line

# The scripted planner's plan, as in crashtest.py: three tool calls then DONE.
TOOL_STEPS = 3
FRESH_CALLS = TOOL_STEPS + 1

# Long enough that the thief finishes a whole run inside it, short enough that a
# failing test does not take a minute to say so.
STALL_MS = 20000
STEAL_AFTER_S = 2.0

SESSION = f"{int(time.time()):x}"


def launch(run_id: str, **knobs: str) -> subprocess.Popen:
    """A worker, left running so the test can decide when to wait for it."""
    return subprocess.Popen(
        [sys.executable, "-m", "agent_runtime"],
        env=child_env(run_id, **knobs),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def settle(worker: subprocess.Popen, timeout: float) -> tuple[int, str, str]:
    """Wait for a worker and hand back what it said."""
    try:
        out, err = worker.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        worker.kill()
        out, err = worker.communicate()
        return -1, out, err
    return worker.returncode, out, err


# --------------------------------------------------------------------------
# what both cases must end up with
# --------------------------------------------------------------------------


async def check_one_run_happened(
    conn: asyncpg.Connection,
    failures: list[str],
    run_id: str,
    calls_spent: int,
    allowed_calls: tuple[int, ...],
) -> None:
    """The run must look exactly as though one worker had done it alone.

    This is the point of the whole mechanism. Two workers touched this run, and
    the evidence they leave behind has to be indistinguishable from one worker
    doing the work once.
    """
    served = {key for key in await remote_keys() if key.startswith(f"{run_id}:")}
    wanted = {f"{run_id}:{step}" for step in range(TOOL_STEPS)}
    check(
        failures,
        served == wanted,
        f"the remote served {sorted(served)}, expected {sorted(wanted)}:"
        " a step ran twice, or one never ran",
    )

    journal = await conn.fetch(
        "select seq, status, event_type from journal_events where run_id = $1 order by seq",
        run_id,
    )
    seqs = [row["seq"] for row in journal]
    check(
        failures,
        seqs == list(range(FRESH_CALLS)),
        f"journal steps {seqs}, expected 0..{FRESH_CALLS - 1} with no gaps and no repeats",
    )
    check(
        failures,
        all(row["status"] == "confirmed" for row in journal),
        "the journal holds a step still at intent",
    )

    effects = await conn.fetch(
        "select seq, status, result from side_effects where run_id = $1 order by seq", run_id
    )
    check(
        failures,
        [row["seq"] for row in effects] == list(range(TOOL_STEPS)),
        f"tool calls at steps {[row['seq'] for row in effects]}, expected 0..{TOOL_STEPS - 1}",
    )
    check(
        failures,
        all(row["status"] == "confirmed" and row["result"] for row in effects),
        "a tool call is unconfirmed or has no result",
    )

    status = await conn.fetchval("select status from runs where run_id = $1", run_id)
    check(failures, status == "done", f"run status is {status!r}, expected 'done'")

    check(
        failures,
        calls_spent in allowed_calls,
        f"the run spent {calls_spent} planner calls, expected one of"
        f" {list(allowed_calls)}: a superseded worker paid for work nobody kept",
    )


# --------------------------------------------------------------------------
# the two cases
# --------------------------------------------------------------------------


async def case_claim(conn: asyncpg.Connection) -> list[str]:
    """Both workers start together. Exactly one of them finishes the run.

    Where the loser dies is not fixed, and must not be asserted. If the two hit
    the compare-and-swap together, one matches no rows and is fenced on the claim
    itself. If process startup skews them apart, the second reads the newer epoch
    and takes the run legitimately, and the first finds out on its next write.
    Both orderings are correct; what matters is that only one worker's work
    survives.
    """
    run_id = f"rt-{SESSION}-claim"
    failures: list[str] = []

    banner("claim  two workers start together")
    line("expected:  one finishes the run, the other is fenced, whether that")
    line("           happens on the claim or on its first write afterwards")

    await truncate(conn)
    calls_before = await mock_calls()

    first, second = launch(run_id), launch(run_id)
    outs = [settle(first, timeout=120), settle(second, timeout=120)]
    calls_spent = await mock_calls() - calls_before

    codes = sorted(code for code, _, _ in outs)
    done = [out for _, out, _ in outs if find(out, "done") is not None]
    fenced = [find(out, "fenced") for _, out, _ in outs if find(out, "fenced") is not None]

    check(failures, len(done) == 1, f"{len(done)} workers reached done, expected 1")
    check(failures, len(fenced) == 1, f"{len(fenced)} workers were fenced, expected 1")
    check(failures, codes == [0, 1], f"exit codes {codes}, expected one 0 and one 1")
    for loser in fenced:
        check(
            failures,
            loser.get("epoch") != loser.get("observed"),
            f"a fenced line reported epoch {loser.get('epoch')} and observed"
            f" {loser.get('observed')}, which should differ",
        )
    line(f"the loser was fenced on: {', '.join(f.get('write', '?') for f in fenced) or 'nothing'}")

    # A worker fenced after buying an answer it never journaled costs the run one
    # extra call, exactly as a crash in that window does. Nobody acted on it.
    await check_one_run_happened(
        conn, failures, run_id, calls_spent, (FRESH_CALLS, FRESH_CALLS + 1)
    )
    report(failures)
    return failures


async def case_steal(conn: asyncpg.Connection) -> list[str]:
    """One worker journals a decision, stalls, and is overtaken."""
    run_id = f"rt-{SESSION}-steal"
    failures: list[str] = []

    banner("steal  a stalled owner is overtaken mid-run")
    line("expected:  the thief finishes the run, and the original wakes to find")
    line("           its next write refused and nothing of its own overwritten")

    await truncate(conn)
    calls_before = await mock_calls()

    # The original claims, journals step 0, and sleeps holding the run.
    original = launch(run_id, STALL_AT="after_decide", CRASH_SEQ="0", STALL_MS=str(STALL_MS))
    await asyncio.sleep(STEAL_AFTER_S)

    thief = launch(run_id)
    thief_code, thief_out, thief_err = settle(thief, timeout=120)

    # The whole case rests on the original still being asleep here. If it has
    # already exited, the stall was too short and nothing below proves anything.
    check(
        failures,
        original.poll() is None,
        "the stalled worker exited before the thief finished, so this case proves nothing",
    )
    settled_before = await snapshot(conn, run_id)

    original_code, original_out, _ = settle(original, timeout=STALL_MS / 1000 + 60)
    settled_after = await snapshot(conn, run_id)
    calls_spent = await mock_calls() - calls_before

    check(
        failures,
        thief_code == 0,
        f"the thief exited {thief_code}, expected 0\n{thief_err.strip()[-400:]}",
    )
    check(
        failures,
        original_code == 1,
        f"the stalled worker exited {original_code}, expected 1: a superseded"
        " worker must abandon the run, not carry on",
    )
    check_steal_logs(failures, original_out, thief_out)

    # The claim the whole mechanism exists to support.
    check(
        failures,
        settled_after == settled_before,
        "the superseded worker changed a confirmed row after waking:\n"
        f"  before {settled_before}\n  after  {settled_after}",
    )

    await check_one_run_happened(conn, failures, run_id, calls_spent, (FRESH_CALLS,))
    report(failures)
    return failures


def check_steal_logs(failures: list[str], original_out: str, thief_out: str) -> None:
    """What the two transcripts have to say about who held the run and when."""
    stalled = find(original_out, "claimed") or {}
    stole = find(thief_out, "claimed") or {}
    check(
        failures,
        bool(stalled.get("epoch") and stole.get("epoch"))
        and int(stole["epoch"]) > int(stalled["epoch"]),
        f"the thief claimed epoch {stole.get('epoch')}, which should be above the"
        f" stalled worker's {stalled.get('epoch')}",
    )
    check(failures, find(thief_out, "done") is not None, "the thief never reached done")

    fenced = find(original_out, "fenced") or {}
    check(failures, bool(fenced), "the stalled worker was never fenced")
    check(
        failures,
        fenced.get("observed") == stole.get("epoch"),
        f"the fenced line observed epoch {fenced.get('observed')}, expected the"
        f" thief's {stole.get('epoch')}",
    )
    check(
        failures,
        count(original_out, "stalled") == 0,
        "the stalled worker logged 'stalled', which means it still owned the run:"
        " a miss for some reason other than being superseded",
    )


def report(failures: list[str]) -> None:
    """Print what went wrong with a case, or that nothing did."""
    for failure in failures:
        line(f"FAIL {failure}")
    line("PASS" if not failures else f"FAILED {len(failures)} check(s)")


CASES = {"claim": case_claim, "steal": case_steal}


async def main() -> None:
    """Run the chosen cases and report which of them failed."""
    parser = argparse.ArgumentParser(description="Two workers on one run.")
    parser.add_argument(
        "cases",
        nargs="*",
        choices=tuple(CASES),
        metavar="CASE",
        help=f"which cases to run (default: all). One of: {', '.join(CASES)}",
    )
    args = parser.parse_args()

    names = args.cases or list(CASES)
    conn = await asyncpg.connect(config.DATABASE_URL)
    failed = []
    try:
        for name in names:
            if await CASES[name](conn):
                failed.append(name)
    finally:
        await conn.close()

    banner(f"FENCING RACE  ran={len(names)} failed={len(failed)}")
    for name in failed:
        line(f"FAILED {name}")
    if failed:
        sys.exit(1)
    line("two workers touched each run, and each run looks like the work of one")


if __name__ == "__main__":
    asyncio.run(main())
