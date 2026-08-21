"""Running one tool call so a crash cannot hide it.

The intent row commits before the call. Its absence afterwards proves nobody
made the call; its presence means the outcome is unknown, which recovery can
resolve. Nothing here interprets tool_name or tool_args.
"""

import json
from typing import Any

import asyncpg

import tools
from crashpoints import crash
from logs import log


def key_for(run_id: str, seq: int) -> str:
    """An agent may call the same tool with the same args at two steps. Only the
    step number separates them, and it survives a crash in the journal."""
    return f"{run_id}:{seq}"


async def write_confirm(
    conn: asyncpg.Connection, idempotency_key: str, result: str, epoch: int
) -> None:
    """Guarded by epoch and by status, so a superseded worker matches zero rows
    instead of overwriting the owner's state."""
    tag = await conn.execute(
        """
        update side_effects
           set status = 'confirmed', result = $2, confirmed_at = now()
         where idempotency_key = $1
           and epoch = $3
           and status = 'intent'
        """,
        idempotency_key,
        result,
        epoch,
    )
    if tag.split()[-1] == "0":
        log("fenced", key=idempotency_key, epoch=epoch)
        raise SystemExit(f"superseded: confirm for {idempotency_key} matched no rows")


async def write_intent(
    conn: asyncpg.Connection,
    run_id: str,
    seq: int,
    tool_name: str,
    tool_args: dict[str, Any],
    epoch: int,
) -> str:
    idempotency_key = key_for(run_id, seq)
    await conn.execute(
        """
        insert into side_effects
            (idempotency_key, run_id, seq, tool_name, tool_args, status, epoch)
        values ($1, $2, $3, $4, $5::jsonb, 'intent', $6)
        """,
        idempotency_key,
        run_id,
        seq,
        tool_name,
        json.dumps(tool_args),
        epoch,
    )
    log("intent", run=run_id, seq=seq, key=idempotency_key, tool=tool_name)
    return idempotency_key


async def flag(conn: asyncpg.Connection, idempotency_key: str, epoch: int) -> None:
    await conn.execute(
        """
        update side_effects
           set status = 'flagged'
         where idempotency_key = $1 and epoch = $2 and status = 'intent'
        """,
        idempotency_key,
        epoch,
    )


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

    await write_confirm(conn, idempotency_key, result, epoch)
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
