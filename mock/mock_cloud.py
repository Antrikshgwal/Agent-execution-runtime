"""A stand-in cloud provisioning API that dedupes on the idempotency key.

Models the one property the runtime relies on from a real provider: a repeated
request carrying a key that has already been served performs no new work and
returns the original result. That is what makes a retry after a crash safe.
"""

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

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


@app.post("/provision")
def provision(req: ProvisionRequest):
    """Create the resource, or return the existing result for a seen key."""
    if req.idempotency_key in created:
        return {"status": "already_done", "result": created[req.idempotency_key]}

    prefix = PREFIXES.get(req.tool_name, "res")
    resource_id = f"{prefix}-{len(created) + 1:07d}"
    created[req.idempotency_key] = resource_id
    return {"status": "created", "result": resource_id}


@app.get("/created")
def get_created():
    """Return every key served so far, for inspecting a run after the fact."""
    return created
