"""A durable agent loop.

Each step asks the planner what to do, journals the answer, then runs the chosen
tool under an idempotency key derived from the step number. Both external actions
follow the same three-step pattern: commit an intent row, act, commit a confirm.

On restart the runtime replays journaled decisions instead of asking again, and
re-sends stranded tool calls under their original keys.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

import asyncpg

import demo_tools  # noqa: F401  (importing registers the demo tools)
import planner
import tools

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://durable:durable@localhost:5433/durable"
)

RUN_ID = os.environ.get("RUN_ID", "run1")
GOAL = os.environ.get("GOAL", "stand up a web service")

# Guard against a planner that never says DONE.
MAX_STEPS = 12

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


def describe(decision: dict[str, Any]) -> str:
    return "DONE" if decision.get("done") else decision["tool_name"]


async def llm_calls(conn: asyncpg.Connection, run_id: str) -> int:
    """Planner calls this run has spent, counted in the database.

    Incremented before each call, so it survives a crash mid-call and reflects
    money spent rather than answers received. A real provider offers nothing
    equivalent to read after a restart.
    """
    return await conn.fetchval(
        "select coalesce(sum(llm_attempts), 0) from journal_events where run_id = $1",
        run_id,
    )


# --------------------------------------------------------------------------
# journal
# --------------------------------------------------------------------------


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


async def decide_step(
    conn: asyncpg.Connection, run_id: str, seq: int, epoch: int
) -> dict[str, Any]:
    """Return this step's decision, replaying it when the journal already holds one."""
    row = await conn.fetchrow(
        "select event_type, payload, status from journal_events where run_id = $1 and seq = $2",
        run_id,
        seq,
    )

    if row is not None and row["status"] == "confirmed":
        # The outcome is known because we wrote it down. Replay it and leave the
        # planner alone. Asking again could return a different plan, and every
        # later step would build on the divergence.
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
        await conn.execute(
            """
            insert into journal_events (run_id, seq, event_type, payload, status, epoch)
            values ($1, $2, 'decision', '{}'::jsonb, 'intent', $3)
            """,
            run_id,
            seq,
            epoch,
        )
    # A row in 'intent' means the planner may have answered and we lost it.
    # Nothing exists to replay, so this step gets decided again. It costs one
    # call, and correctness holds because nobody acted on the lost answer.

    # Count the attempt before making it. A crash during the call still leaves
    # the spend recorded.
    await conn.execute(
        "update journal_events set llm_attempts = llm_attempts + 1 where run_id = $1 and seq = $2",
        run_id,
        seq,
    )
    log("deciding", run=run_id, seq=seq, llm_calls=await llm_calls(conn, run_id))
    history = await load_history(conn, run_id)
    decision = await planner.decide(GOAL, history, tools.schemas_for_model())

    crash("after_decide_before_journal")

    event_type = "done" if decision.get("done") else "decision"
    tag = await conn.execute(
        """
        update journal_events
           set payload = $3::jsonb, event_type = $4, status = 'confirmed', confirmed_at = now()
         where run_id = $1 and seq = $2 and epoch = $5 and status = 'intent'
        """,
        run_id,
        seq,
        json.dumps(decision),
        event_type,
        epoch,
    )
    if tag.split()[-1] == "0":
        log("fenced", run=run_id, seq=seq, epoch=epoch)
        raise SystemExit(f"superseded: journal confirm for {run_id}:{seq} matched no rows")

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


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------


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
    tools.get(tool_name).validate(tool_args)

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
         where idempotency_key = $1 and epoch = $2 and status = 'intent'
        """,
        idempotency_key,
        epoch,
    )


# --------------------------------------------------------------------------
# recovery
# --------------------------------------------------------------------------


async def recover(conn: asyncpg.Connection, run_id: str, epoch: int) -> tuple[set[int], set[int]]:
    """Resolve every tool call left in the unknown window.

    Journaled decisions are handled by decide_step as the loop reaches them.
    Returns the steps whose tool call needs no further work, and the steps a
    human has to resolve before the run may continue.
    """
    rows = await conn.fetch(
        "select * from side_effects where run_id = $1 order by seq", run_id
    )
    journal = await conn.fetch(
        "select status from journal_events where run_id = $1", run_id
    )

    settled: set[int] = set()
    blocked: set[int] = set()
    reconciled = 0
    calls_before = await llm_calls(conn, run_id)

    banner(f"RECOVERY run={run_id} epoch={epoch}")
    jcounts = {"intent": 0, "confirmed": 0}
    for row in journal:
        jcounts[row["status"]] = jcounts.get(row["status"], 0) + 1
    counts = {"intent": 0, "confirmed": 0, "flagged": 0}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    print(
        f"  journal: steps={len(journal)}  confirmed={jcounts['confirmed']}"
        f"  intent={jcounts['intent']}",
        flush=True,
    )
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
                f" args={row['tool_args']} created_at={row['created_at']}  (needs a human)",
                flush=True,
            )
            blocked.add(seq)
            continue

        if not tools.get(tool_name).supports_idempotency_key:
            # No key means no safe retry. A second attempt could create a second
            # resource, and no request distinguishes a lost response from a lost
            # request. Hand it to a human instead.
            await flag(conn, idempotency_key, epoch)
            log("flagged", key=idempotency_key, reason="remote has no idempotency key")
            print(
                f"  FLAGGED key={idempotency_key} tool={tool_name}"
                f" args={row['tool_args']} created_at={row['created_at']}  (needs a human)",
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

    calls_after = await llm_calls(conn, run_id)
    print(
        f"  llm_calls before={calls_before} after={calls_after}"
        f"  ({'unchanged' if calls_before == calls_after else 'CHANGED'})",
        flush=True,
    )
    banner(f"RECOVERY COMPLETE  reconciled={reconciled} flagged={len(blocked)}")
    return settled, blocked


# --------------------------------------------------------------------------


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

        log("registry", tools=",".join(tools.REGISTRY), planner=planner.BACKEND)
        settled, blocked = await recover(conn, RUN_ID, epoch)

        for seq in range(MAX_STEPS):
            if seq in blocked:
                log("blocked", run=RUN_ID, seq=seq, reason="flagged step needs a human")
                await conn.execute(
                    "update runs set status = 'failed' where run_id = $1 and epoch = $2",
                    RUN_ID,
                    epoch,
                )
                return

            decision = await decide_step(conn, RUN_ID, seq, epoch)

            if decision.get("done"):
                await conn.execute(
                    "update runs set status = 'done' where run_id = $1 and epoch = $2",
                    RUN_ID,
                    epoch,
                )
                log("done", run=RUN_ID, steps=seq, llm_calls=await llm_calls(conn, RUN_ID))
                return

            if seq in settled:
                log("skipped", run=RUN_ID, seq=seq, reason="tool call already confirmed")
                continue

            await run_step(conn, RUN_ID, seq, decision["tool_name"], decision["tool_args"], epoch)

        log("guard", run=RUN_ID, reason=f"reached MAX_STEPS={MAX_STEPS} without DONE")
        await conn.execute(
            "update runs set status = 'failed' where run_id = $1 and epoch = $2", RUN_ID, epoch
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
