"""Running one tool call so a crash cannot hide it.

The intent row commits before the call. Its absence afterwards proves nobody
made the call; its presence means the outcome is unknown, which recovery can
resolve. Nothing here interprets tool_name or tool_args.
"""

import json
from typing import Any

import asyncpg

from agent_runtime import fencing, tools
from agent_runtime.crashpoints import crash
from agent_runtime.logs import log


def key_for(run_id: str, seq: int) -> str:
    """An agent may call the same tool with the same args at two steps. Only the
    step number separates them, and it survives a crash in the journal."""
    return f"{run_id}:{seq}"


async def write_confirm(
    conn: asyncpg.Connection, run_id: str, idempotency_key: str, result: str, epoch: int
) -> None:
    """Guarded by ownership and by status, so a superseded worker matches zero
    rows instead of overwriting the owner's state.

    Recovery re-sends a call the previous epoch left stranded, so the row being
    confirmed is often stamped with an epoch nobody holds any more. See
    fencing.py for why the guard asks about `runs` rather than about that stamp.
    """
    tag = await conn.execute(
        """
        update side_effects s
           set status = 'confirmed', result = $2, confirmed_at = now(), epoch = $3
         where s.idempotency_key = $1
           and s.status = 'intent'
           and exists (select 1 from runs r where r.run_id = s.run_id and r.epoch = $3)
        """,
        idempotency_key,
        result,
        epoch,
    )
    if not fencing.matched(tag):
        await fencing.superseded(conn, run_id, epoch, f"confirm for {idempotency_key}")


async def write_intent(
    conn: asyncpg.Connection,
    run_id: str,
    seq: int,
    tool_name: str,
    tool_args: dict[str, Any],
    epoch: int,
) -> str:
    """Commit the intent row, but only while this worker still owns the run.

    An unguarded insert here lets a superseded worker create a row the owner will
    later collide with on the primary key, or strand one nobody will confirm.
    Fencing before the row exists also means fencing before the call goes out.
    """
    idempotency_key = key_for(run_id, seq)
    tag = await conn.execute(
        """
        insert into side_effects
            (idempotency_key, run_id, seq, tool_name, tool_args, status, epoch)
        select $1, $2, $3, $4, $5::jsonb, 'intent', $6
         where exists (select 1 from runs r where r.run_id = $2 and r.epoch = $6)
        """,
        idempotency_key,
        run_id,
        seq,
        tool_name,
        json.dumps(tool_args),
        epoch,
    )
    if not fencing.matched(tag):
        await fencing.superseded(conn, run_id, epoch, f"intent for {idempotency_key}")

    log("intent", run=run_id, seq=seq, key=idempotency_key, tool=tool_name)
    return idempotency_key


async def flag(
    conn: asyncpg.Connection, run_id: str, idempotency_key: str, epoch: int
) -> None:
    """Hand a call to a human, durably.

    The row count matters as much as the write. Without it a flag that matched
    nothing still logs as though it succeeded, and the row sits at `intent` for
    every restart that follows to flag again.
    """
    tag = await conn.execute(
        """
        update side_effects s
           set status = 'flagged', epoch = $2
         where s.idempotency_key = $1
           and s.status = 'intent'
           and exists (select 1 from runs r where r.run_id = s.run_id and r.epoch = $2)
        """,
        idempotency_key,
        epoch,
    )
    if not fencing.matched(tag):
        await fencing.superseded(conn, run_id, epoch, f"flag for {idempotency_key}")


async def call_tool(
    conn: asyncpg.Connection,
    run_id: str,
    seq: int,
    tool_name: str,
    tool_args: dict[str, Any],
    idempotency_key: str,
    epoch: int,
    attempt: int,
) -> tuple[str, str]:
    """Send the call and confirm it. Shared by the first attempt and by recovery,
    so a re-send follows exactly the path the original took."""
    log("calling", run=run_id, seq=seq, key=idempotency_key, attempt=attempt)
    result, remote = await tools.dispatch(tool_name, tool_args, idempotency_key)

    crash("after_call")

    await write_confirm(conn, run_id, idempotency_key, result, epoch)
    log("confirmed", run=run_id, seq=seq, key=idempotency_key, result=result, remote=remote)
    return result, remote


async def run_step(
    conn: asyncpg.Connection,
    run_id: str,
    seq: int,
    tool_name: str,
    tool_args: dict[str, Any],
    epoch: int,
) -> None:
    # Validate before the intent row exists. A malformed decision fails here,
    # where it cannot cause a side effect.
    tools.get(tool_name).validate(tool_args)

    crash("before_intent")
    idempotency_key = await write_intent(conn, run_id, seq, tool_name, tool_args, epoch)
    crash("after_intent")

    await call_tool(
        conn, run_id, seq, tool_name, tool_args, idempotency_key, epoch, attempt=1
    )

    crash("after_confirm")
