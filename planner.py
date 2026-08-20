"""Client for the planner.

The runtime asks for one decision at a time and journals the answer before
acting on it. Nothing here retries. A retry is exactly what recovery must avoid,
so the decision to call or to replay belongs to the runtime.
"""

import os
from typing import Any

import httpx

MOCK_LLM_URL = os.environ.get("MOCK_LLM_URL", "http://localhost:9100")


async def decide(
    goal: str, history: list[dict[str, Any]], schemas: list[dict[str, Any]]
) -> dict[str, Any]:
    """Ask for the next step. Returns {tool_name, tool_args} or {done: true}."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{MOCK_LLM_URL}/decide",
            json={"goal": goal, "history": history, "tools": schemas},
        )
    return response.json()


async def call_count() -> int:
    """How many times anyone has asked the planner to decide."""
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(f"{MOCK_LLM_URL}/calls")
    return response.json()["calls"]
