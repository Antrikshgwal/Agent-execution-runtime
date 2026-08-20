"""Intent-first tool execution with crash recovery.

The runtime writes a committed intent row before every tool call and confirms
after, and on startup it resolves any row left in the unknown window by
re-sending under the same idempotency key.

Tools arrive through the registry. The plan below stands in for the LLM's
decisions until phase 5 journals them.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

import asyncpg

import demo_tools  # noqa: F401  (importing registers the demo tools)
import tools

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://durable:durable@localhost:5433/durable"
)

RUN_ID = os.environ.get("RUN_ID", "run1")
GOAL = "stand up a web service"

# Stand-in for journaled LLM decisions. Position in this list is the step's seq,
# which is what the idempotency key is derived from.
PLAN: list[tuple[str, dict[str, Any]]] = [
    ("create_server", {"name": "srv-231", "spec": "t3.micro"}),
    ("create_database", {"name": "app-db", "engine": "postgres", "size_gb": 20}),
    ("create_dns_record", {"hostname": "app.example.com", "record_type": "A", "target": "srv-231"}),
]

# Hard-kill injection. Phase 6 builds the crash test suite around these names.
# The point of setting them here is that the crashing path and the normal path
# are the same path.
CRASH_AT = os.environ.get("CRASH_AT")


def log(event: str, **fields: Any) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    stamp = stamp.replace("+00:00", "Z")
    pairs = "  ".join(f"{key}={value}" for key, value in fields.items())
    print(f"{stamp} INFO  {event:<10} {pairs}", flush=True)


def banner(text: str) -> None:
    print(f"================ {text} ================", flush=True)


def crash(point: str) -> None:
    """Hard kill, modelling kill -9: no unwinding, no finally, no flush."""
    if CRASH_AT != point:
        return
    print(f"!!! CRASH_AT={point} -- os._exit(1)", flush=True)
    sys.stdout.flush()
    os._exit(1)


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


async def run_step(
    conn: asyncpg.Connection,
    run_id: str,
    seq: int,
    tool_name: str,
    tool_args: dict[str, Any],
    epoch: int,
) -> None:
    idempotency_key = key_for(run_id, seq)

    # Validate before the intent row exists. A malformed decision fails here,
    # where it cannot cause a side effect.
    entry = tools.get(tool_name)
    entry.validate(tool_args)

    crash("before_intent")

    # Intent commits before any network call. Its absence later proves nobody
    # made the call; its presence means the outcome is unknown.
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

    crash("after_intent")

    log("calling", run=run_id, seq=seq, key=idempotency_key, attempt=1)
    result, remote = await tools.dispatch(tool_name, tool_args, idempotency_key)

    crash("after_call")

    await write_confirm(conn, idempotency_key, result, epoch)
    log("confirmed", run=run_id, seq=seq, key=idempotency_key, result=result, remote=remote)

    crash("after_confirm")


async def flag(conn: asyncpg.Connection, idempotency_key: str, epoch: int) -> None:
    await conn.execute(
        """
        update side_effects
           set status = 'flagged'
         where idempotency_key = $1
           and epoch = $2
           and status = 'intent'
        """,
        idempotency_key,
        epoch,
    )


async def recover(conn: asyncpg.Connection, run_id: str, epoch: int) -> tuple[set[int], set[int]]:
    """Resolve every row left in the unknown window.

    Returns the steps needing no further work, and the steps a human has to
    resolve before the run may continue.
    """
    rows = await conn.fetch(
        "select * from side_effects where run_id = $1 order by seq",
        run_id,
    )

    settled: set[int] = set()
    blocked: set[int] = set()
    reconciled = 0

    banner(f"RECOVERY run={run_id} epoch={epoch}")
    counts = {"intent": 0, "confirmed": 0, "flagged": 0}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    print(
        f"  tools:   calls={len(rows)}  confirmed={counts['confirmed']}"
        f"  intent={counts['intent']}  flagged={counts['flagged']}",
        flush=True,
    )

    for row in rows:
        seq = row["seq"]
        idempotency_key = row["idempotency_key"]
        tool_name = row["tool_name"]

        if row["status"] == "confirmed":
            settled.add(seq)
            continue

        if row["status"] == "flagged":
            print(
                f"  FLAGGED key={idempotency_key} tool={tool_name}"
                f" args={row['tool_args']} created_at={row['created_at']}"
                "  (needs a human)",
                flush=True,
            )
            blocked.add(seq)
            continue

        # status == 'intent': the outcome is unknown.
        if not tools.get(tool_name).supports_idempotency_key:
            # No key means no safe retry. A second attempt could create a second
            # resource, and no request distinguishes a lost response from a lost
            # request. Hand it to a human instead.
            await flag(conn, idempotency_key, epoch)
            log("flagged", key=idempotency_key, reason="remote has no idempotency key")
            print(
                f"  FLAGGED key={idempotency_key} tool={tool_name}"
                f" args={row['tool_args']} created_at={row['created_at']}"
                "  (needs a human)",
                flush=True,
            )
            blocked.add(seq)
            continue

        # Re-sending is safe: the key matches the lost attempt, so the remote
        # either performs the work for the first time or replays its result.
        print(
            f"  reconcile key={idempotency_key} status=intent -> re-sending with same key",
            flush=True,
        )
        log("calling", run=run_id, seq=seq, key=idempotency_key, attempt=2)
        result, remote = await tools.dispatch(
            tool_name, json.loads(row["tool_args"]), idempotency_key
        )
        await write_confirm(conn, idempotency_key, result, epoch)
        log("confirmed", run=run_id, seq=seq, key=idempotency_key, result=result, remote=remote)
        print(
            f"  RECONCILED key={idempotency_key} result={result} remote={remote}"
            f"  ({'no new resource' if remote == 'already_done' else 'first execution'})",
            flush=True,
        )
        settled.add(seq)
        reconciled += 1

    banner(f"RECOVERY COMPLETE  reconciled={reconciled} flagged={len(blocked)}")
    return settled, blocked


async def main() -> None:
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(
            """
            insert into runs (run_id, goal, status) values ($1, $2, 'running')
            on conflict (run_id) do nothing
            """,
            RUN_ID,
            GOAL,
        )
        epoch = await conn.fetchval("select epoch from runs where run_id = $1", RUN_ID)

        log("registry", tools=",".join(tools.REGISTRY))

        settled, blocked = await recover(conn, RUN_ID, epoch)

        for seq, (tool_name, tool_args) in enumerate(PLAN):
            if seq in blocked:
                log("blocked", run=RUN_ID, seq=seq, reason="flagged step needs a human")
                await conn.execute(
                    "update runs set status = 'failed' where run_id = $1 and epoch = $2",
                    RUN_ID,
                    epoch,
                )
                break
            if seq in settled:
                log("skipped", run=RUN_ID, seq=seq, reason="already confirmed")
                continue
            await run_step(conn, RUN_ID, seq, tool_name, tool_args, epoch)
        else:
            await conn.execute(
                "update runs set status = 'done' where run_id = $1 and epoch = $2",
                RUN_ID,
                epoch,
            )
            log("done", run=RUN_ID, steps=len(PLAN))
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
