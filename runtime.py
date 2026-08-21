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
    fencing.py    who owns the run, and what happens to everyone else
    tools.py      the registry the loop dispatches through
"""

import asyncio

import asyncpg

import config
import crashpoints
import demo_tools  # noqa: F401  (importing registers the demo tools)
import executor
import fencing
import planner
import recovery
import tools
from journal import decide_step, llm_calls
from logs import log


async def finish(conn: asyncpg.Connection, run_id: str, epoch: int, status: str) -> None:
    """Close the run out, if this worker is still the one entitled to.

    The row count is the whole check. Without it a superseded worker's verdict
    vanishes silently, and the run it no longer owns looks to that worker like it
    ended the way that worker thought it did.
    """
    tag = await conn.execute(
        "update runs set status = $2 where run_id = $1 and epoch = $3",
        run_id,
        status,
        epoch,
    )
    if not fencing.matched(tag):
        await fencing.superseded(conn, run_id, epoch, f"finish as {status}")


async def run(conn: asyncpg.Connection, run_id: str, epoch: int) -> None:
    """Settle what a crash left behind, then step until the agent is done."""
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
    """Claim the configured run and work it to a conclusion."""
    conn = await asyncpg.connect(config.DATABASE_URL)
    try:
        log("registry", tools=",".join(tools.REGISTRY), planner=planner.BACKEND)
        epoch = await fencing.claim(conn, config.RUN_ID, config.GOAL, config.WORKER)
        await run(conn, config.RUN_ID, epoch)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
