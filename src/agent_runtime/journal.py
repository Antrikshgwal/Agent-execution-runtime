"""The agent's memory: what it decided, and what it spent deciding.

Every decision is committed here before the runtime acts on it, so a restart can
replay the decision instead of asking the planner again. Asking again could
return a different plan, and each later step would build on the divergence.
"""

import json
from typing import Any

import asyncpg

from agent_runtime import config, fencing, planner, tools
from agent_runtime.crashpoints import crash
from agent_runtime.logs import log


def describe(decision: dict[str, Any]) -> str:
    return "DONE" if decision.get("done") else decision["tool_name"]


async def llm_calls(conn: asyncpg.Connection, run_id: str) -> int:
    """Planner calls this run has spent, counted in the database.

    Incremented before each call, so it survives a crash mid-call and reflects
    money spent rather than answers received. A real provider offers nothing
    equivalent to read after a restart.
    """
    # sum() over no rows is null, coalesce turns that into 0, and an aggregate
    # always returns exactly one row. int() states that for the type checker.
    return int(
        await conn.fetchval(
            "select coalesce(sum(llm_attempts), 0) from journal_events where run_id = $1",
            run_id,
        )
    )


async def load_history(conn: asyncpg.Connection, run_id: str) -> list[dict[str, Any]]:
    """Rebuild what the agent knows from committed rows.

    In-memory history is never authoritative. Everything the planner sees comes
    back out of the journal, joined to the results its tool calls produced.
    """
    decisions = await conn.fetch(
        """
        select seq, payload from journal_events
         where run_id = $1 and status = 'confirmed' and event_type = 'decision'
         order by seq
        """,
        run_id,
    )
    results = {
        row["seq"]: row["result"]
        for row in await conn.fetch(
            "select seq, result from side_effects where run_id = $1 and status = 'confirmed'",
            run_id,
        )
    }

    history = []
    for row in decisions:
        entry: dict[str, Any] = {"seq": row["seq"], "decision": json.loads(row["payload"])}
        if row["seq"] in results:
            entry["result"] = results[row["seq"]]
        history.append(entry)
    return history


async def _open_step(conn: asyncpg.Connection, run_id: str, seq: int, epoch: int) -> None:
    """Commit the intent row that precedes the planner call.

    Guarded, so a superseded worker cannot append a step at a `seq` the owner has
    already passed, and cannot reach the planner call that follows.
    """
    tag = await conn.execute(
        """
        insert into journal_events (run_id, seq, event_type, payload, status, epoch)
        select $1, $2, 'decision', '{}'::jsonb, 'intent', $3
         where exists (select 1 from runs r where r.run_id = $1 and r.epoch = $3)
        """,
        run_id,
        seq,
        epoch,
    )
    if not fencing.matched(tag):
        await fencing.superseded(conn, run_id, epoch, f"journal intent for {run_id}:{seq}")


async def _count_attempt(conn: asyncpg.Connection, run_id: str, seq: int, epoch: int) -> None:
    """Charge this worker for the planner call it is about to make.

    Guarded for two reasons. It is a write, so a superseded worker has no
    business making it; and it sits immediately before the call, so failing here
    is what stops that worker from spending money on a run it no longer owns.
    """
    tag = await conn.execute(
        """
        update journal_events j
           set llm_attempts = llm_attempts + 1
         where j.run_id = $1 and j.seq = $2
           and exists (select 1 from runs r where r.run_id = j.run_id and r.epoch = $3)
        """,
        run_id,
        seq,
        epoch,
    )
    if not fencing.matched(tag):
        await fencing.superseded(conn, run_id, epoch, f"attempt count for {run_id}:{seq}")


async def _confirm_step(
    conn: asyncpg.Connection,
    run_id: str,
    seq: int,
    decision: dict[str, Any],
    epoch: int,
) -> None:
    """Record the answer, guarded by whether this worker still owns the run.

    A restart re-decides a step the previous epoch left at `intent`, so the row
    is often stamped with an epoch nobody holds. See fencing.py for why the
    guard asks about `runs` rather than about that stamp.
    """
    tag = await conn.execute(
        """
        update journal_events j
           set payload = $3::jsonb, event_type = $4, status = 'confirmed',
               confirmed_at = now(), epoch = $5
         where j.run_id = $1 and j.seq = $2 and j.status = 'intent'
           and exists (select 1 from runs r where r.run_id = j.run_id and r.epoch = $5)
        """,
        run_id,
        seq,
        json.dumps(decision),
        "done" if decision.get("done") else "decision",
        epoch,
    )
    if not fencing.matched(tag):
        await fencing.superseded(conn, run_id, epoch, f"journal confirm for {run_id}:{seq}")


async def decide_step(
    conn: asyncpg.Connection, run_id: str, seq: int, epoch: int
) -> dict[str, Any]:
    """Return this step's decision, replaying it when the journal holds one."""
    row = await conn.fetchrow(
        "select payload, status from journal_events where run_id = $1 and seq = $2",
        run_id,
        seq,
    )

    if row is not None and row["status"] == "confirmed":
        decision = json.loads(row["payload"])
        log(
            "replayed",
            run=run_id,
            seq=seq,
            tool=describe(decision),
            args=json.dumps(decision.get("tool_args", {})),
            llm_calls=await llm_calls(conn, run_id),
        )
        return decision

    if row is None:
        crash("before_decide")
        await _open_step(conn, run_id, seq, epoch)
    # A row in 'intent' means the planner may have answered and we lost it.
    # Nothing exists to replay, so this step gets decided again. It costs one
    # call, and correctness holds because nobody acted on the lost answer.

    # Count the attempt before making it, so a crash during the call still
    # leaves the spend recorded.
    await _count_attempt(conn, run_id, seq, epoch)
    log("deciding", run=run_id, seq=seq, llm_calls=await llm_calls(conn, run_id))

    history = await load_history(conn, run_id)
    decision = await planner.decide(config.GOAL, history, tools.schemas_for_model())

    crash("after_decide_before_journal")

    await _confirm_step(conn, run_id, seq, decision, epoch)
    log(
        "decided",
        run=run_id,
        seq=seq,
        tool=describe(decision),
        args=json.dumps(decision.get("tool_args", {})),
        llm_calls=await llm_calls(conn, run_id),
    )

    crash("after_decide")
    return decision
