"""Planner backends.

Two of them, chosen by PLANNER:

  mock    the scripted HTTP stub in mock/, deterministic and free. Crash tests
          use it, because a test that pays a provider and picks a different
          plan each run proves nothing.
  gemini  a real model. Replay stops being a simulation here: ask twice and the
          second answer may differ, which is the hazard the journal exists for.

Nothing in this module retries. Deciding whether to call or to replay belongs to
the runtime, and a retry hidden down here would defeat it.
"""

import asyncio
import os
from pathlib import Path
from typing import Any

import httpx

_ENV_FILE = Path(__file__).with_name(".env.local")
if _ENV_FILE.exists():
    from dotenv import load_dotenv

    load_dotenv(_ENV_FILE)

BACKEND = os.environ.get("PLANNER", "mock")
MOCK_LLM_URL = os.environ.get("MOCK_LLM_URL", "http://localhost:9100")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")

SYSTEM = (
    "You are provisioning infrastructure toward a goal. Choose exactly one tool "
    "call per turn, using what earlier steps already created. Call finish when "
    "the goal is met. Do not repeat a step that already succeeded."
)


async def decide(
    goal: str, history: list[dict[str, Any]], schemas: list[dict[str, Any]]
) -> dict[str, Any]:
    """Ask for the next step. Returns {tool_name, tool_args} or {done: true}."""
    if BACKEND == "mock":
        return await _decide_mock(goal, history, schemas)
    if BACKEND == "gemini":
        return await _decide_gemini(goal, history, schemas)
    raise ValueError(f"unknown PLANNER={BACKEND!r}, expected 'mock' or 'gemini'")


# --------------------------------------------------------------------------
# mock
# --------------------------------------------------------------------------


async def _decide_mock(
    goal: str, history: list[dict[str, Any]], schemas: list[dict[str, Any]]
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{MOCK_LLM_URL}/decide",
            json={"goal": goal, "history": history, "tools": schemas},
        )
    return response.json()


async def mock_call_count() -> int:
    """The stub's own counter, useful as a cross-check against the journal."""
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(f"{MOCK_LLM_URL}/calls")
    return response.json()["calls"]


# --------------------------------------------------------------------------
# gemini
# --------------------------------------------------------------------------

FINISH = "finish"


def _render(goal: str, history: list[dict[str, Any]]) -> str:
    lines = [f"Goal: {goal}", ""]
    if not history:
        lines.append("No steps have run yet.")
    else:
        lines.append("Steps so far:")
        for entry in history:
            decision = entry["decision"]
            if decision.get("done"):
                continue
            outcome = entry.get("result", "no result recorded")
            lines.append(
                f"  {entry['seq']}. {decision['tool_name']}({decision['tool_args']})"
                f" -> {outcome}"
            )
    lines += ["", "Choose the next tool call, or call finish."]
    return "\n".join(lines)


def _tool_config(schemas: list[dict[str, Any]]):
    from google.genai import types

    declarations = [
        types.FunctionDeclaration(
            name=schema["name"],
            description=schema["description"],
            parameters_json_schema=schema["parameters"],
        )
        for schema in schemas
    ]
    declarations.append(
        types.FunctionDeclaration(
            name=FINISH,
            description="Call when the goal is complete and no further tool calls are needed.",
            parameters_json_schema={"type": "object", "properties": {}},
        )
    )
    return [types.Tool(function_declarations=declarations)]


def _decide_gemini_blocking(
    goal: str, history: list[dict[str, Any]], schemas: list[dict[str, Any]]
) -> dict[str, Any]:
    from google import genai
    from google.genai import types

    client = genai.Client()
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=_render(goal, history),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            tools=_tool_config(schemas),
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="ANY")
            ),
        ),
    )

    calls = response.function_calls or []
    if not calls:
        # Forced function calling should make this unreachable. Treat a bare
        # text answer as the model declining to act rather than as a step.
        return {"done": True, "note": "planner returned no function call"}

    call = calls[0]
    if call.name == FINISH:
        return {"done": True}
    return {"tool_name": call.name, "tool_args": dict(call.args or {})}


async def _decide_gemini(
    goal: str, history: list[dict[str, Any]], schemas: list[dict[str, Any]]
) -> dict[str, Any]:
    # The SDK call is blocking, so keep it off the event loop.
    return await asyncio.to_thread(_decide_gemini_blocking, goal, history, schemas)
