"""A durable agent loop.

Each step asks the planner what to do, journals the answer, then runs the chosen
tool under an idempotency key derived from the step number. Both external actions
follow the same three-step pattern: commit an intent row, act, commit a confirm.

On restart the runtime replays journaled decisions instead of asking again, and
re-sends stranded tool calls under their original keys.

    runtime.py    this loop
    journal.py    decisions, replay, and what the planner cost
    executor.py   one tool call, intent-first
    recovery.py   what a crash left behind
    tools.py      the registry the loop dispatches through
"""

import asyncio

import asyncpg

import config
import crashpoints
import demo_tools  # noqa: F401  (importing registers the demo tools)
import executor
import planner
import recovery
import tools
from journal import decide_step, llm_calls
from logs import log


async def claim(conn: asyncpg.Connection, run_id: str, goal: str) -> int:
    """Start or resume the run, returning the epoch that guards its writes."""
    await conn.execute(
        """
        insert into runs (run_id, goal, status) values ($1, $2, 'running')
        on conflict (run_id) do nothing
        """,
        run_id,
        goal,
    )
    epoch = await conn.fetchval("select epoch from runs where run_id = $1", run_id)
    if epoch is None:
        # The insert above put the row there. Nothing in this runtime deletes a
        # run, so a miss here means something outside it did.
        raise RuntimeError(f"run {run_id} vanished between its insert and its read")
    return epoch


async def finish(conn: asyncpg.Connection, run_id: str, epoch: int, status: str) -> None:
    await conn.execute(
        "update runs set status = $2 where run_id = $1 and epoch = $3",
        run_id,
        status,
        epoch,
    )


async def run(conn: asyncpg.Connection, run_id: str, epoch: int) -> None:
    outcome = await recovery.recover(conn, run_id, epoch)

    for seq in range(config.MAX_STEPS):
        crashpoints.at_step(seq)

        if seq in outcome.blocked:
            # The agent's next decision would be conditioned on a result nobody
            # holds, so the run stops here for a human.
            log("blocked", run=run_id, seq=seq, reason="flagged step needs a human")
            await finish(conn, run_id, epoch, "failed")
            return

        decision = await decide_step(conn, run_id, seq, epoch)

        if decision.get("done"):
            await finish(conn, run_id, epoch, "done")
            log("done", run=run_id, steps=seq, llm_calls=await llm_calls(conn, run_id))
            return

        if seq in outcome.settled:
            log("skipped", run=run_id, seq=seq, reason="tool call already confirmed")
            continue

        await executor.run_step(
            conn, run_id, seq, decision["tool_name"], decision["tool_args"], epoch
        )

    log("guard", run=run_id, reason=f"reached MAX_STEPS={config.MAX_STEPS} without DONE")
    await finish(conn, run_id, epoch, "failed")


async def main() -> None:
    conn = await asyncpg.connect(config.DATABASE_URL)
    try:
        log("registry", tools=",".join(tools.REGISTRY), planner=planner.BACKEND)
        epoch = await claim(conn, config.RUN_ID, config.GOAL)
        await run(conn, config.RUN_ID, epoch)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
