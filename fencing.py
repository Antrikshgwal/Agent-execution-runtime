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
