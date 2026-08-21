"""Whether the worker holding this epoch still owns the run.

Every write the runtime makes to `journal_events` or `side_effects` carries the
epoch its worker was handed at claim time, and asks the same question in its
`where` clause:

    and exists (select 1 from runs r where r.run_id = ... and r.epoch = $n)

The question is about `runs`, not about the row being written. A row's own
`epoch` column records which worker wrote it, which is history: recovery
routinely settles a row an earlier epoch left behind, and comparing the worker's
epoch against that stamp would make it fence itself against its own crashed
predecessor. Each guarded write restamps the row as it passes, so the column
stays an accurate record of who last touched it.

The check belongs inside the `where` clause. A worker can be overtaken between a
read-then-write pair, but not inside a single conditional statement.

A guarded write that matches no rows has two possible causes, and the log has to
say which. Another worker may have claimed the run, or the row may not have been
in the state the write required. Both are fatal, and `superseded` tells them
apart by reading the live epoch back.
"""

from typing import NoReturn

import asyncpg

from logs import log


def matched(tag: str) -> bool:
    """Whether a command tag from asyncpg reports a row.

    Covers 'UPDATE 1' and 'INSERT 0 1' alike: the count is always last.
    """
    return tag.split()[-1] != "0"


async def current_epoch(conn: asyncpg.Connection, run_id: str) -> int | None:
    """The epoch the run carries now, which a stale worker's own epoch is not.

    Only ever read to explain a write that already failed. A worker must never
    read this to decide whether to write: it would pick up the value the winner
    installed and wave its own stale write through.
    """
    return await conn.fetchval("select epoch from runs where run_id = $1", run_id)


async def claim(conn: asyncpg.Connection, run_id: str, goal: str, owner: str) -> int:
    """Take the run, and return the epoch that guards every write that follows.

    Read the epoch, then bump it conditioned on the value read. Postgres
    serializes two updates on the same row, so of two workers that read the same
    epoch exactly one finds its `where` clause still true. The other matches no
    rows and learns it lost before it has written anything.

    The read and the bump are separate statements because `on conflict do
    nothing` returns no row on conflict, so the insert cannot report the epoch of
    a run that already existed. Splitting them is safe: whatever happens between
    the two, the bump is conditioned on what the read saw.

    A worker that arrives after another has finished claiming reads the newer
    epoch and takes the run from it. That is deliberate. A crashed worker leaves
    no signal that it died, so a restart has to be able to take its own run back,
    and fencing is what makes the previous owner harmless if it was only stalled.
    `claimed_at` is recorded but nothing reads it: as written it says when the
    claim happened, not when the owner was last alive, and only a heartbeat would
    make it mean the second thing.
    """
    await conn.execute(
        # The goal is fixed for the life of the run, since replay depends on it
        # not changing. A resumed run keeps the one it was created with.
        """
        insert into runs (run_id, goal, status) values ($1, $2, 'running')
        on conflict (run_id) do nothing
        """,
        run_id,
        goal,
    )

    observed = await current_epoch(conn, run_id)
    if observed is None:
        raise RuntimeError(f"run {run_id} vanished between its insert and its read")

    epoch = await conn.fetchval(
        """
        update runs
           set epoch = epoch + 1, owner = $2, claimed_at = now()
         where run_id = $1 and epoch = $3
        returning epoch
        """,
        run_id,
        owner,
        observed,
    )
    if epoch is None:
        # Someone claimed between the read and the bump. Report it the same way
        # as any other write that matched no rows, because that is what it is.
        await superseded(conn, run_id, observed, "claim")

    log("claimed", run=run_id, epoch=epoch, owner=owner)
    return epoch


async def superseded(
    conn: asyncpg.Connection, run_id: str, epoch: int, write: str
) -> NoReturn:
    """Report a guarded write that matched no rows, and abandon the run."""
    observed = await current_epoch(conn, run_id)

    if observed != epoch:
        log("fenced", run=run_id, epoch=epoch, observed=observed, write=write)
        raise SystemExit(
            f"superseded: {write} matched no rows;"
            f" {run_id} moved from epoch {epoch} to {observed}"
        )

    # Still the owner, so the run did not change hands and the row was not in the
    # state the write required. That is a bug in the runtime, not a lost race,
    # and calling it 'fenced' would send whoever reads the log after the wrong
    # thing entirely.
    log("stalled", run=run_id, epoch=epoch, observed=observed, write=write)
    raise SystemExit(
        f"{write} matched no rows for {run_id} at epoch {epoch},"
        " though the run is still ours"
    )
