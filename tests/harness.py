"""What both proof harnesses need to read the world back.

The runtime's log is an interface here, not decoration. Asserting on it as well
as on the database is deliberate: the log is what a human reads after an
incident, so a claim it cannot support is a claim the design has not really made.
"""

import os
from typing import Any

import asyncpg
import httpx

MOCK_CLOUD_URL = os.environ.get("MOCK_CLOUD_URL", "http://localhost:9000")
MOCK_LLM_URL = os.environ.get("MOCK_LLM_URL", "http://localhost:9100")


def check(failures: list[str], ok: bool, message: str) -> None:
    """Record a failure, so one case reports everything wrong with it at once."""
    if not ok:
        failures.append(message)


async def mock_calls() -> int:
    """The planner's own counter, the instrument for the agent proof."""
    async with httpx.AsyncClient(timeout=10) as client:
        return (await client.get(f"{MOCK_LLM_URL}/calls")).json()["calls"]


async def remote_keys() -> dict[str, str]:
    """Every idempotency key the remote has served, mapped to what it returned."""
    async with httpx.AsyncClient(timeout=10) as client:
        return (await client.get(f"{MOCK_CLOUD_URL}/created")).json()


def events(output: str) -> list[tuple[str, dict[str, str]]]:
    """Parse the runtime's log lines back into (event, fields)."""
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


def count(output: str, event: str) -> int:
    """How many times an event was logged."""
    return sum(1 for name, _ in events(output) if name == event)


def child_env(run_id: str, **extra: str) -> dict[str, str]:
    """A worker's environment, with the injection knobs cleared unless asked for.

    Cleared rather than merely unset, because a knob left over in the parent's
    environment would arm a fault the test never intended and quietly change what
    it proves.
    """
    env = dict(os.environ, RUN_ID=run_id, PLANNER="mock")
    for knob in ("CRASH_AT", "STALL_AT", "CRASH_SEQ", "STALL_MS"):
        env.pop(knob, None)
    env.update(extra)
    return env


async def truncate(conn: asyncpg.Connection) -> None:
    """Every table, in one statement so the foreign keys do not object."""
    await conn.execute("truncate side_effects, journal_events, runs")


async def snapshot(conn: asyncpg.Connection, run_id: str) -> list[dict[str, Any]]:
    """The confirmed tool calls, in a form two points in time can be compared by.

    A superseded worker must not overwrite one of these. Comparing the rows
    before and after it wakes is the only way to see that it did not, since a
    write that lands leaves the row looking perfectly ordinary.
    """
    rows = await conn.fetch(
        """
        select idempotency_key, status, result, confirmed_at, epoch
          from side_effects
         where run_id = $1
         order by seq
        """,
        run_id,
    )
    return [dict(row) for row in rows]
