"""A stand-in cloud provisioning API that dedupes on the idempotency key.

Models the one property the runtime relies on from a real provider: a repeated
request carrying a key that has already been served performs no new work and
returns the original result. That is what makes a retry after a crash safe.
"""

import os
import time
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

# Seconds to hold a response open before returning it. Zero by default, because
# the crash tests spawn dozens of processes and every second here is paid many
# times over. Set MOCK_DELAY=5 to crash the agent by hand: a real provider takes
# long enough to be killed mid-call, and this is the only thing that does not.
DELAY = float(os.environ.get("MOCK_DELAY", "0"))

created: dict[str, str] = {}

# Result ids are prefixed per tool only so a demo transcript is readable.
# The idempotency key is the sole determinant of whether work is performed.
PREFIXES = {
    "create_server": "i",
    "create_database": "db",
    "create_dns_record": "dns",
}


class ProvisionRequest(BaseModel):
    """One provisioning call: what to create, and the key that identifies it."""

    tool_name: str
    tool_args: dict[str, Any]
    idempotency_key: str


def _slow() -> None:
    """Hold the response open, if MOCK_DELAY asked for it.

    The handler is a plain `def`, so FastAPI runs it in a worker thread and this
    blocks one request rather than the whole server.
    """
    if DELAY:
        time.sleep(DELAY)


@app.post("/provision")
def provision(req: ProvisionRequest):
    """Create the resource, or return the existing result for a seen key."""
    if req.idempotency_key in created:
        _slow()
        return {"status": "already_done", "result": created[req.idempotency_key]}

    prefix = PREFIXES.get(req.tool_name, "res")
    resource_id = f"{prefix}-{len(created) + 1:07d}"
    created[req.idempotency_key] = resource_id
    # The delay lands after the resource exists, so a caller killed while waiting
    # leaves the work done and the answer lost. Delaying first would only ever
    # produce a call that never happened, which is the easy half of the problem.
    _slow()
    return {"status": "created", "result": resource_id}


@app.get("/created")
def get_created():
    """Return every key served so far, for inspecting a run after the fact."""
    return created
