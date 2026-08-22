"""Resolving what a crash left behind.

Runs on startup before any new work. Journaled decisions are handled by
journal.decide_step as the loop reaches them; this module settles the tool calls,
whose outcome the database alone cannot determine.
"""

import json
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from agent_runtime import executor, tools
from agent_runtime.journal import llm_calls
from agent_runtime.logs import banner, line, log


@dataclass
class Outcome:
    """What the loop needs to know after recovery."""

    settled: set[int] = field(default_factory=set)
    blocked: set[int] = field(default_factory=set)
    reconciled: int = 0


def _report_flagged(row: asyncpg.Record) -> None:
    line(
        f"FLAGGED key={row['idempotency_key']} tool={row['tool_name']}"
        f" args={row['tool_args']} created_at={row['created_at']}  (needs a human)"
    )


async def _reconcile(
    conn: asyncpg.Connection, row: asyncpg.Record, run_id: str, epoch: int
) -> None:
    """Re-send under the original key.

    Safe because the key matches the lost attempt, so the remote either performs
    the work for the first time or returns what it stored the first time.
    """
    idempotency_key = row["idempotency_key"]
    line(f"reconcile key={idempotency_key} status=intent -> re-sending with same key")

    _, remote = await executor.call_tool(
        conn,
        run_id,
        row["seq"],
        row["tool_name"],
        json.loads(row["tool_args"]),
        idempotency_key,
        epoch,
        attempt=2,
    )
    result = await conn.fetchval(
        "select result from side_effects where idempotency_key = $1", idempotency_key
    )
    verdict = "no new resource" if remote == "already_done" else "first execution"
    line(f"RECONCILED key={idempotency_key} result={result} remote={remote}  ({verdict})")


async def _summarise(conn: asyncpg.Connection, run_id: str, rows: list[asyncpg.Record]) -> None:
    journal = await conn.fetch(
        "select status from journal_events where run_id = $1", run_id
    )
    steps = _tally(journal, ("intent", "confirmed"))
    calls = _tally(rows, ("intent", "confirmed", "flagged"))
    line(
        f"journal: steps={len(journal)}  confirmed={steps['confirmed']}"
        f"  intent={steps['intent']}"
    )
    line(
        f"tools:   calls={len(rows)}  confirmed={calls['confirmed']}"
        f"  intent={calls['intent']}  flagged={calls['flagged']}"
    )


def _tally(rows: list[asyncpg.Record], statuses: tuple[str, ...]) -> dict[str, int]:
    counts = dict.fromkeys(statuses, 0)
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return counts


async def recover(conn: asyncpg.Connection, run_id: str, epoch: int) -> Outcome:
    rows = await conn.fetch(
        "select * from side_effects where run_id = $1 order by seq", run_id
    )
    outcome = Outcome()
    calls_before = await llm_calls(conn, run_id)

    banner(f"RECOVERY run={run_id} epoch={epoch}")
    await _summarise(conn, run_id, rows)

    for row in rows:
        seq, status = row["seq"], row["status"]

        if status == "confirmed":
            outcome.settled.add(seq)
            continue

        if status == "flagged":
            _report_flagged(row)
            outcome.blocked.add(seq)
            continue

        if not tools.get(row["tool_name"]).supports_idempotency_key:
            # No key means no safe retry. A second attempt could create a second
            # resource, and no request distinguishes a lost response from a lost
            # request. Hand it to a human instead.
            await executor.flag(conn, run_id, row["idempotency_key"], epoch)
            log(
                "flagged",
                key=row["idempotency_key"],
                reason="remote has no idempotency key",
            )
            _report_flagged(row)
            outcome.blocked.add(seq)
            continue

        await _reconcile(conn, row, run_id, epoch)
        outcome.settled.add(seq)
        outcome.reconciled += 1

    calls_after = await llm_calls(conn, run_id)
    verdict = "unchanged" if calls_before == calls_after else "CHANGED"
    line(f"llm_calls before={calls_before} after={calls_after}  ({verdict})")
    banner(
        f"RECOVERY COMPLETE  reconciled={outcome.reconciled} flagged={len(outcome.blocked)}"
    )
    return outcome
