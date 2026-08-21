"""The assert protocol for the crash points.

Each case runs the agent twice over one run: once with CRASH_AT set, so the
process dies at a named point, and once without, so recovery resolves whatever
the first process left behind. EXPECTED fixes what the pair must produce, and
every number is checked against the mocks' own counters as well as against the
database, because the runtime counting its own calls proves less than the
planner counting them.

The two proofs are two rows of that table:

    after_call    the resource count must not move across the restart, because
                  recovery re-sent the original idempotency key
    after_decide  the planner counter must not move across the restart, because
                  the runtime replayed the journal instead of asking again

Both mocks must stay up for the whole run. Restarting mock_cloud wipes the key
map, restarting mock_llm resets the counter, and either one voids the result.

    python crashtest.py                 every point, crashing on the first step
    python crashtest.py --seq 2         every point, crashing partway through
    python crashtest.py after_call      one point
"""

import argparse
import asyncio
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import httpx

import config
from crashpoints import POINTS
from logs import banner, line

HERE = Path(__file__).parent
MOCK_CLOUD_URL = os.environ.get("MOCK_CLOUD_URL", "http://localhost:9000")
MOCK_LLM_URL = os.environ.get("MOCK_LLM_URL", "http://localhost:9100")

# The scripted planner's plan: three tool calls, then DONE. A run that finishes
# clean therefore costs four planner calls, journals four steps, and creates
# three resources. Every expectation below is written against these.
TOOL_STEPS = 3
FRESH_CALLS = TOOL_STEPS + 1

# One token per invocation. The mock cloud keeps its key map for as long as it
# is up, and it must: restarting it between the crash and the restart would hand
# the tool proof its answer for free. The cost is that a second invocation would
# otherwise meet the keys the first one left and be told already_done where it
# expected created, so every run works under names no earlier run has used.
SESSION = f"{int(time.time()):x}"


@dataclass(frozen=True)
class Expect:
    """What one crash point must produce. Mirrors the crash-point table in specs.md."""

    paid_for_the_step: bool  # the dying process spent a planner call on the step it died on
    replayed: bool  # the restart replays that decision rather than buying it again
    # The attempt number the restart sends under and the verdict the remote gives
    # it, or None where the tool is not called again. One field because they are
    # one fact: after_intent and after_call differ only in the verdict.
    restart_call: tuple[int, str] | None
    reconciled: int  # rows recovery re-sent under their original key
    state: str  # what the crash left behind
    after: str  # what recovery must produce

    def total_calls(self) -> int:
        """Planner calls across both processes.

        A clean run's four, plus one if the crash bought an answer that nothing
        can replay. Every other point costs exactly what a clean run costs.
        """
        wasted = 1 if self.paid_for_the_step and not self.replayed else 0
        return FRESH_CALLS + wasted

    def crash_run_calls(self, seq: int) -> int:
        """Planner calls the dying process spent: one for each step it decided."""
        return seq + (1 if self.paid_for_the_step else 0)


EXPECTED: dict[str, Expect] = {
    "before_decide": Expect(
        paid_for_the_step=False,
        replayed=False,
        restart_call=(1, "created"),
        reconciled=0,
        state="no journal row for the step, so nobody called the planner",
        after="the step is decided fresh and the run costs what a clean run costs",
    ),
    "after_decide_before_journal": Expect(
        paid_for_the_step=True,
        replayed=False,
        restart_call=(1, "created"),
        reconciled=0,
        state="journal row at intent, the decision lost, no side effect",
        after="nothing to replay, so the step is decided again: one call more than a"
        " clean run, and correctness holds because nobody acted on the lost answer",
    ),
    "after_decide": Expect(
        paid_for_the_step=True,
        replayed=True,
        restart_call=(1, "created"),
        reconciled=0,
        state="journal row confirmed, no side_effects row, the tool never called",
        after="AGENT PROOF: the decision is replayed, the planner counter holds flat,"
        " and the tool runs once",
    ),
    "before_intent": Expect(
        paid_for_the_step=True,
        replayed=True,
        restart_call=(1, "created"),
        reconciled=0,
        state="the decision is journaled and no side_effects row exists yet",
        after="the decision is replayed and the tool runs once, as a first attempt",
    ),
    "after_intent": Expect(
        paid_for_the_step=True,
        replayed=True,
        restart_call=(2, "created"),
        reconciled=1,
        state="side_effects row at intent, the remote never reached",
        after="re-sent under the same key; the remote answers created, because this is"
        " the first time it has seen the key",
    ),
    "after_call": Expect(
        paid_for_the_step=True,
        replayed=True,
        restart_call=(2, "already_done"),
        reconciled=1,
        state="the resource exists, the row is still intent, result is null",
        after="TOOL PROOF: re-sent under the same key, the remote answers already_done,"
        " no second resource appears, and the result matches the pre-crash id",
    ),
    "after_confirm": Expect(
        paid_for_the_step=True,
        replayed=True,
        restart_call=None,
        reconciled=0,
        state="the row is confirmed with its result set",
        after="recovery skips it: no calling line for that key on the restart",
    ),
}


# --------------------------------------------------------------------------
# reading the world back
# --------------------------------------------------------------------------


async def mock_calls() -> int:
    """The planner's own counter, the instrument for the agent proof."""
    async with httpx.AsyncClient(timeout=10) as client:
        return (await client.get(f"{MOCK_LLM_URL}/calls")).json()["calls"]


async def remote_keys() -> dict[str, str]:
    """Every idempotency key the remote has served, mapped to what it returned."""
    async with httpx.AsyncClient(timeout=10) as client:
        return (await client.get(f"{MOCK_CLOUD_URL}/created")).json()


def spawn(run_id: str, crash_at: str | None, crash_seq: int) -> subprocess.CompletedProcess:
    """One agent process. Blocking on purpose: the point is that it dies."""
    env = dict(os.environ, RUN_ID=run_id, PLANNER="mock", CRASH_SEQ=str(crash_seq))
    if crash_at:
        env["CRASH_AT"] = crash_at
    else:
        env.pop("CRASH_AT", None)
    return subprocess.run(
        [sys.executable, "runtime.py"],
        cwd=HERE,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def events(output: str) -> list[tuple[str, dict[str, str]]]:
    """Parse the runtime's log lines back into (event, fields).

    Asserting on the log as well as on the database is deliberate. The log is
    what a human reads after an incident, so a claim it cannot support is a
    claim the design has not really made.
    """
    parsed = []
    for raw in output.splitlines():
        _, marker, rest = raw.partition(" INFO ")
        if not marker:
            continue
        event, _, tail = rest.strip().partition(" ")
        fields = {}
        for chunk in tail.split("  "):
            name, sep, value = chunk.strip().partition("=")
            if sep:
                fields[name] = value
        parsed.append((event, fields))
    return parsed


def find(output: str, event: str, **match: str) -> dict[str, str] | None:
    """The first logged event whose fields include every given pair."""
    for name, fields in events(output):
        if name == event and all(fields.get(k) == v for k, v in match.items()):
            return fields
    return None


def recovery_totals(output: str) -> tuple[int, int]:
    """The reconciled and flagged counts off the recovery block's closing banner."""
    found = re.search(r"RECOVERY COMPLETE\s+reconciled=(\d+) flagged=(\d+)", output)
    return (int(found.group(1)), int(found.group(2))) if found else (-1, -1)


# --------------------------------------------------------------------------
# one case
# --------------------------------------------------------------------------


def check(failures: list[str], ok: bool, message: str) -> None:
    """Record a failure, so one case reports everything wrong with it at once."""
    if not ok:
        failures.append(message)


@dataclass(frozen=True)
class Case:
    """One row of EXPECTED, aimed at one step of one run."""

    point: str
    seq: int
    expect: Expect
    run_id: str

    @property
    def key(self) -> str:
        """The idempotency key of the step the crash lands on."""
        return f"{self.run_id}:{self.seq}"


@dataclass(frozen=True)
class Observed:
    """What the pair of processes and the two mocks say actually happened.

    Gathered before anything is checked, because the crash run's numbers have to
    be read while they are still the crash run's: the restart moves both counters.
    """

    crashed: subprocess.CompletedProcess
    restarted: subprocess.CompletedProcess
    crash_calls: int  # planner calls the dying process spent
    restart_calls: int  # planner calls the restart spent
    keys_at_crash: dict[str, str]  # what the remote had served when the process died
    keys_after: dict[str, str]  # what it had served once recovery finished

    @property
    def total_calls(self) -> int:
        """Planner calls across both processes."""
        return self.crash_calls + self.restart_calls


async def observe(conn: asyncpg.Connection, case: Case) -> Observed:
    """Run the case: a clean table, a process that dies, and one that recovers."""
    await conn.execute("truncate side_effects, journal_events, runs")

    calls_before = await mock_calls()
    crashed = spawn(case.run_id, crash_at=case.point, crash_seq=case.seq)
    calls_at_crash = await mock_calls()
    keys_at_crash = await remote_keys()

    restarted = spawn(case.run_id, crash_at=None, crash_seq=case.seq)
    calls_after = await mock_calls()

    return Observed(
        crashed=crashed,
        restarted=restarted,
        crash_calls=calls_at_crash - calls_before,
        restart_calls=calls_after - calls_at_crash,
        keys_at_crash=keys_at_crash,
        keys_after=await remote_keys(),
    )


def check_processes(failures: list[str], seen: Observed, case: Case) -> None:
    """That the crash landed where it was aimed and the restart finished."""
    check(
        failures,
        f"CRASH_AT={case.point} seq={case.seq}" in seen.crashed.stdout,
        f"the run never reached {case.point} at step {case.seq}, so this case proves nothing",
    )
    for label, process, expected in (
        ("crash run", seen.crashed, 1),
        ("restart", seen.restarted, 0),
    ):
        stderr = process.stderr.strip()
        check(
            failures,
            process.returncode == expected,
            f"{label} exited {process.returncode}, expected {expected}"
            + (f"\n{stderr[-400:]}" if stderr else ""),
        )


async def check_planner(
    failures: list[str], conn: asyncpg.Connection, seen: Observed, case: Case
) -> None:
    """What the planner was asked, and whether the runtime agrees about it."""
    expect = case.expect
    check(
        failures,
        seen.crash_calls == expect.crash_run_calls(case.seq),
        f"the dying process spent {seen.crash_calls} planner calls,"
        f" expected {expect.crash_run_calls(case.seq)}",
    )
    check(
        failures,
        seen.total_calls == expect.total_calls(),
        f"the run spent {seen.total_calls} planner calls, expected {expect.total_calls()}"
        f" (a clean run costs {FRESH_CALLS})",
    )
    if expect.replayed:
        check(
            failures,
            find(seen.restarted.stdout, "replayed", run=case.run_id, seq=str(case.seq))
            is not None,
            f"no replayed event for step {case.seq}:"
            " the restart re-decided a journaled step",
        )

    # The runtime counts its own calls in the database so the number survives a
    # kill. Nothing keeps that honest but the planner's counter, so compare them.
    counted = await conn.fetchval(
        "select coalesce(sum(llm_attempts), 0) from journal_events where run_id = $1",
        case.run_id,
    )
    check(
        failures,
        counted == seen.total_calls,
        f"the database counted {counted} planner calls,"
        f" the planner counted {seen.total_calls}",
    )


def check_remote(failures: list[str], seen: Observed, case: Case) -> set[str]:
    """What the remote was asked, and under which keys. Returns the keys it served."""
    served = {name for name in seen.keys_after if name.startswith(f"{case.run_id}:")}
    wanted = {f"{case.run_id}:{step}" for step in range(TOOL_STEPS)}
    check(
        failures,
        served == wanted,
        f"the remote served {sorted(served)}, expected {sorted(wanted)}",
    )
    check(
        failures,
        len(seen.keys_after) - len(seen.keys_at_crash) == len(wanted - set(seen.keys_at_crash)),
        "the remote gained a key that no step asked for",
    )

    if case.expect.restart_call is None:
        check(
            failures,
            find(seen.restarted.stdout, "calling", key=case.key) is None,
            f"the restart called {case.key} again, though it was already confirmed",
        )
    else:
        attempt, verdict = case.expect.restart_call
        check(
            failures,
            find(seen.restarted.stdout, "calling", key=case.key, attempt=str(attempt))
            is not None,
            f"no calling event for {case.key} at attempt {attempt}",
        )
        check(
            failures,
            find(seen.restarted.stdout, "confirmed", key=case.key, remote=verdict) is not None,
            f"the remote did not answer {verdict} for {case.key}",
        )
    return served


async def check_tool_proof(
    failures: list[str], conn: asyncpg.Connection, seen: Observed, case: Case
) -> None:
    """The whole claim: the resource that survives is the one the lost call made,
    not a second one wearing its name."""
    before_id = seen.keys_at_crash.get(case.key)
    stored = await conn.fetchval(
        "select result from side_effects where idempotency_key = $1", case.key
    )
    check(
        failures,
        before_id is not None,
        f"the remote never recorded {case.key} before the crash",
    )
    check(
        failures,
        stored == before_id,
        f"stored result {stored!r} is not the pre-crash id {before_id!r}",
    )


async def run_case(conn: asyncpg.Connection, point: str, seq: int) -> list[str]:
    """Crash at one point, recover, and check everything the pair must produce."""
    case = Case(
        point=point,
        seq=seq,
        expect=EXPECTED[point],
        # A distinct run per case and per invocation, because the mock cloud
        # answers any key it has already served. Reusing a run_id would let one
        # case's keys answer the next, and the remote would report work nobody
        # in this run did.
        run_id=f"ct-{SESSION}-{point}-s{seq}",
    )
    failures: list[str] = []

    banner(f"{point}  seq={seq}")
    line(f"at crash:  {case.expect.state}")
    line(f"expected:  {case.expect.after}")

    seen = await observe(conn, case)
    check_processes(failures, seen, case)
    await check_planner(failures, conn, seen, case)
    served = check_remote(failures, seen, case)

    reconciled, flagged = recovery_totals(seen.restarted.stdout)
    check(
        failures,
        reconciled == case.expect.reconciled,
        f"recovery reconciled {reconciled} rows, expected {case.expect.reconciled}",
    )
    check(failures, flagged == 0, f"recovery flagged {flagged} rows, expected none")

    if point == "after_call":
        await check_tool_proof(failures, conn, seen, case)

    failures += await check_tables(conn, case.run_id)

    line(
        f"planner: crash-run={seen.crash_calls}  restart={seen.restart_calls}"
        f"  total={seen.total_calls}  (clean run costs {FRESH_CALLS})"
    )
    line(f"remote:  keys={len(served)}  reconciled={reconciled}  flagged={flagged}")
    for failure in failures:
        line(f"FAIL {failure}")
    line("PASS" if not failures else f"FAILED {len(failures)} check(s)")
    return failures


async def check_tables(conn: asyncpg.Connection, run_id: str) -> list[str]:
    """What the run must have left behind, whatever route it took to get there."""
    failures: list[str] = []

    journal = await conn.fetch(
        "select seq, event_type, status from journal_events where run_id = $1 order by seq",
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
    done = [row["seq"] for row in journal if row["event_type"] == "done"]
    check(
        failures,
        done == [TOOL_STEPS],
        f"expected one done event at step {TOOL_STEPS}, got {done}",
    )

    effects = await conn.fetch(
        "select seq, status, result from side_effects where run_id = $1 order by seq", run_id
    )
    steps = [row["seq"] for row in effects]
    check(
        failures,
        steps == list(range(TOOL_STEPS)),
        f"tool calls at steps {steps}, expected 0..{TOOL_STEPS - 1}",
    )
    check(
        failures,
        all(row["status"] == "confirmed" for row in effects),
        "a tool call is still at intent after recovery",
    )
    check(failures, all(row["result"] for row in effects), "a confirmed tool call has no result")

    status = await conn.fetchval("select status from runs where run_id = $1", run_id)
    check(failures, status == "done", f"run status is {status!r}, expected 'done'")
    return failures


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


async def main() -> None:
    """Run the chosen points and report which of them failed."""
    parser = argparse.ArgumentParser(description="Run the crash points and assert on the result.")
    parser.add_argument(
        "points",
        nargs="*",
        choices=POINTS,
        metavar="POINT",
        help=f"which points to run (default: all). One of: {', '.join(POINTS)}",
    )
    parser.add_argument(
        "--seq",
        type=int,
        default=0,
        help=f"which step to crash on, 0..{TOOL_STEPS - 1} (default: 0). A later step"
        " leaves the restart with settled work behind it as well as ahead of it.",
    )
    args = parser.parse_args()

    if not 0 <= args.seq < TOOL_STEPS:
        parser.error(f"--seq must be 0..{TOOL_STEPS - 1}: only those steps make a tool call")

    points = args.points or list(POINTS)
    conn = await asyncpg.connect(config.DATABASE_URL)
    failed = []
    try:
        for point in points:
            if await run_case(conn, point, args.seq):
                failed.append(point)
    finally:
        await conn.close()

    banner(f"CRASH TESTS  ran={len(points)} failed={len(failed)} seq={args.seq}")
    for point in failed:
        line(f"FAILED {point}")
    if failed:
        sys.exit(1)
    line("every crash point recovered to the state its row of the table describes")


if __name__ == "__main__":
    asyncio.run(main())
