"""Pressing each guarded write from both sides of a stolen run.

`claim` steals a run by bumping `runs.epoch`, and two live workers racing for it
settle the question at claim time, before either has written anything. That
leaves the more interesting case untested: a worker that already holds a run,
loses it, and comes back to write. These checks bump the epoch directly, which
reaches that state without having to time two processes against each other.

Both directions matter, and the first is the one easy to get wrong. A worker that
takes over a run must be able to settle rows an earlier epoch stranded, because
that is what recovery does on every restart. A guard comparing the worker's epoch
against the row's stamp would refuse exactly that, and the runtime would fence
itself against its own crashed predecessor.

    python fencetest.py

Needs Postgres. The mocks can stay down: nothing here calls a tool or a planner.
"""

# The point is to press the guards directly, one write at a time, without a
# planner call in the way. Reaching journal's private steps is what that costs.
# pylint: disable=protected-access

import asyncio
import sys
from typing import Any, Awaitable, Callable

import asyncpg

import config
import executor
import journal
from logs import banner, line

TOOL = "create_server"
ARGS = {"name": "srv", "spec": "t3.micro"}
DECISION: dict[str, Any] = {"tool_name": TOOL, "tool_args": ARGS}

RUNS = (
    "fc-adopt",
    "fc-stale-confirm",
    "fc-stale-intent",
    "fc-journal-adopt",
    "fc-journal-stale",
    "fc-spend",
    "fc-owned-miss",
)


async def fresh(conn: asyncpg.Connection, run_id: str) -> None:
    """A run at epoch 0 with nothing behind it."""
    await wipe(conn, run_id)
    await conn.execute(
        "insert into runs (run_id, goal, status, epoch) values ($1, 'goal', 'running', 0)",
        run_id,
    )


async def wipe(conn: asyncpg.Connection, run_id: str) -> None:
    """Remove every trace of a run, so a check starts from nothing."""
    for table in ("side_effects", "journal_events", "runs"):
        await conn.execute(f"delete from {table} where run_id = $1", run_id)


async def steal(conn: asyncpg.Connection, run_id: str) -> None:
    """What another worker's claim does, without the second worker."""
    await conn.execute("update runs set epoch = epoch + 1 where run_id = $1", run_id)


async def fences(label: str, write: Awaitable[Any]) -> bool:
    """The write must refuse to land and stop the worker."""
    try:
        await write
    except SystemExit:
        line(f"PASS  {label}")
        return True
    line(f"FAIL  {label}: the write landed")
    return False


async def lands(label: str, write: Awaitable[Any]) -> bool:
    """The write must go through: this worker still owns the run."""
    try:
        await write
    except SystemExit as exc:
        line(f"FAIL  {label}: fenced when it should not have -- {exc}")
        return False
    line(f"PASS  {label}")
    return True


def holds(label: str, ok: bool) -> bool:
    """A plain assertion about state, rather than about a write."""
    line(f"{'PASS' if ok else 'FAIL'}  {label}")
    return ok


async def check_side_effects(conn: asyncpg.Connection) -> list[bool]:
    """The tool table, from both sides of a steal."""
    results = []

    run = "fc-adopt"
    await fresh(conn, run)
    key = await executor.write_intent(conn, run, 0, TOOL, ARGS, 0)
    await steal(conn, run)
    results.append(
        await lands(
            "the new owner confirms a row the old epoch stranded",
            executor.write_confirm(conn, run, key, "i-1", 1),
        )
    )
    stamp = await conn.fetchval(
        "select epoch from side_effects where idempotency_key = $1", key
    )
    results.append(holds("that confirm restamped the row to the new epoch", stamp == 1))

    run = "fc-stale-confirm"
    await fresh(conn, run)
    key = await executor.write_intent(conn, run, 0, TOOL, ARGS, 0)
    await steal(conn, run)
    results.append(
        await fences(
            "a superseded worker cannot confirm",
            executor.write_confirm(conn, run, key, "i-2", 0),
        )
    )

    run = "fc-stale-intent"
    await fresh(conn, run)
    await steal(conn, run)
    results.append(
        await fences(
            "a superseded worker cannot open an intent row",
            executor.write_intent(conn, run, 0, TOOL, ARGS, 0),
        )
    )
    stranded = await conn.fetchval(
        "select count(*) from side_effects where run_id = $1", run
    )
    results.append(holds("and left no row for the owner to collide with", stranded == 0))
    return results


async def check_journal(conn: asyncpg.Connection) -> list[bool]:
    """The decision table, and the spend that goes with it."""
    results = []

    run = "fc-journal-adopt"
    await fresh(conn, run)
    await journal._open_step(conn, run, 0, 0)
    await steal(conn, run)
    results.append(
        await lands(
            "the new owner confirms a step the old epoch stranded",
            journal._confirm_step(conn, run, 0, DECISION, 1),
        )
    )

    run = "fc-journal-stale"
    await fresh(conn, run)
    await journal._open_step(conn, run, 0, 0)
    await steal(conn, run)
    results.append(
        await fences(
            "a superseded worker cannot confirm a step",
            journal._confirm_step(conn, run, 0, DECISION, 0),
        )
    )

    run = "fc-spend"
    await fresh(conn, run)
    await journal._open_step(conn, run, 0, 0)
    await steal(conn, run)
    results.append(
        await fences(
            "a superseded worker is stopped before it pays for a planner call",
            journal._count_attempt(conn, run, 0, 0),
        )
    )
    spent = await conn.fetchval(
        "select coalesce(sum(llm_attempts), 0) from journal_events where run_id = $1", run
    )
    results.append(holds("and llm_attempts did not move", spent == 0))
    return results


async def check_owned_miss(conn: asyncpg.Connection) -> list[bool]:
    """A guarded write can miss without anyone having stolen the run.

    Confirming a row that is already confirmed matches nothing, and the epoch is
    still ours. Reporting that as `fenced` would send whoever reads the log
    hunting for a second worker that does not exist, so it logs `stalled`.
    """
    run = "fc-owned-miss"
    await fresh(conn, run)
    key = await executor.write_intent(conn, run, 0, TOOL, ARGS, 0)
    await executor.write_confirm(conn, run, key, "i-3", 0)
    return [
        await fences(
            "confirming twice stops the worker, and logs stalled rather than fenced",
            executor.write_confirm(conn, run, key, "i-4", 0),
        )
    ]


async def main() -> None:
    """Run every check and report how many held."""
    conn = await asyncpg.connect(config.DATABASE_URL)
    results: list[bool] = []
    groups: tuple[tuple[str, Callable[[asyncpg.Connection], Any]], ...] = (
        ("side_effects", check_side_effects),
        ("journal_events", check_journal),
        ("a miss that is not a steal", check_owned_miss),
    )
    try:
        for title, group in groups:
            banner(title)
            results += await group(conn)
        for run in RUNS:
            await wipe(conn, run)
    finally:
        await conn.close()

    banner(f"FENCING  {sum(results)}/{len(results)} held")
    if not all(results):
        sys.exit(1)
    line("the owner settles what an earlier epoch stranded, and nobody else writes")


if __name__ == "__main__":
    asyncio.run(main())
