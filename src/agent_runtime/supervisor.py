"""Restarting runs whose worker never came back.

The runtime resumes a run correctly when it is started on one. Nothing in it
notices that it ought to be. A worker that dies leaves `runs.status` at
`running` and stops refreshing `claimed_at`, and the row then sits there looking
exactly like a run being worked on, because a status column cannot tell the
difference between busy and dead.

The heartbeat is what makes the difference visible, and this is what reads it. A
run still at `running` whose `claimed_at` has gone stale had a worker and no
longer does.

    select run_id from runs
     where status = 'running'
       and claimed_at < now() - <lease>

Workers are started as separate processes rather than run in this one. A worker
that loses its claim exits, which is right for something that owns one run and
wrong for something minding several, and a crash in a run should not take the
supervisor down with it. Spawning also means the thing being supervised is the
same `python -m agent_runtime` a person runs.

Starting a worker on a run that turns out to be alive is safe and needs no
locking here. Both workers claim, and the compare-and-swap decides: the newer
epoch proceeds and the older one is fenced at its next write. The lease only
decides when it is worth trying.

    python -m agent_runtime.supervisor
"""

import asyncio
import os
import subprocess
import sys

import asyncpg

from agent_runtime import config
from agent_runtime.logs import log


async def abandoned(conn: asyncpg.Connection, lease: float) -> list[str]:
    """Runs that are still marked running and have stopped beating.

    Ordered oldest first, so the run that has been waiting longest is picked up
    first when several fall over together.
    """
    rows = await conn.fetch(
        """
        select run_id
          from runs
         where status = 'running'
           and claimed_at < now() - make_interval(secs => $1)
         order by claimed_at
        """,
        lease,
    )
    return [row["run_id"] for row in rows]


def start_worker(run_id: str) -> subprocess.Popen:
    """Hand one run to a fresh process."""
    return subprocess.Popen(
        [sys.executable, "-m", "agent_runtime"],
        env={**_clean_env(), "RUN_ID": run_id},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _clean_env() -> dict[str, str]:
    """The supervisor's environment without the fault injection knobs.

    A supervisor started in a shell that still had CRASH_AT set would hand every
    worker it spawned the same crash, and restart them forever.
    """
    env = dict(os.environ)
    for knob in ("CRASH_AT", "STALL_AT", "CRASH_SEQ", "STALL_MS"):
        env.pop(knob, None)
    return env


async def sweep(conn: asyncpg.Connection, running: dict[str, subprocess.Popen]) -> None:
    """One pass: reap finished workers, then start one for each abandoned run."""
    for run_id, worker in list(running.items()):
        if worker.poll() is not None:
            log("worker-exited", run=run_id, code=worker.returncode)
            del running[run_id]

    for run_id in await abandoned(conn, config.LEASE_SECONDS):
        # Already minding it. Its heartbeat may simply not have landed yet.
        if run_id in running:
            continue
        running[run_id] = start_worker(run_id)
        log("resuming", run=run_id, pid=running[run_id].pid)


async def main() -> None:
    """Watch for abandoned runs until interrupted."""
    conn = await asyncpg.connect(config.DATABASE_URL)
    running: dict[str, subprocess.Popen] = {}
    log(
        "supervisor",
        lease=config.LEASE_SECONDS,
        poll=config.POLL_SECONDS,
        heartbeat=config.HEARTBEAT_SECONDS,
    )
    try:
        while True:
            await sweep(conn, running)
            await asyncio.sleep(config.POLL_SECONDS)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log("supervisor-stopping", minding=len(running))
    finally:
        await conn.close()


def run_cli() -> None:
    """Console-script entry point, for the `agent-supervisor` command."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run_cli()
