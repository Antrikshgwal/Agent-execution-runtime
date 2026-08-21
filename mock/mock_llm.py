"""A scripted stand-in for a planner, with a call counter.

The counter is the instrument for the agent proof. Kill the runtime after a
decision and restart it: a flat counter means the runtime replayed the journal,
and a rising one means it asked again.

Every decision embeds the call number in the resource name, so asking again
produces a visibly different plan. That models the real hazard. A second call to
a real model may return something else, and the journal would then describe a
history that never happened.
"""

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

state: dict[str, int] = {"calls": 0}


class DecideRequest(BaseModel):
    """What the planner is given: the goal, the run so far, and the tool set."""

    goal: str
    history: list[dict[str, Any]]
    tools: list[dict[str, Any]]


@app.post("/decide")
def decide(req: DecideRequest):
    """Return the next tool call for this step, or done once the plan is spent."""
    state["calls"] += 1
    call = state["calls"]
    step = len(req.history)

    if step == 0:
        return {
            "tool_name": "create_server",
            "tool_args": {"name": f"srv-{call}", "spec": "t3.micro"},
        }
    if step == 1:
        return {
            "tool_name": "create_database",
            "tool_args": {"name": f"app-db-{call}", "engine": "postgres", "size_gb": 20},
        }
    if step == 2:
        # Reads the server it chose at step 0, which only works if the runtime
        # rebuilt history from the journal.
        server = req.history[0]["decision"]["tool_args"]["name"]
        return {
            "tool_name": "create_dns_record",
            "tool_args": {
                "hostname": f"app-{call}.example.com",
                "record_type": "A",
                "target": server,
            },
        }
    return {"done": True}


@app.get("/calls")
def get_calls():
    """Return how many decisions have been asked for since the process started."""
    return {"calls": state["calls"]}
