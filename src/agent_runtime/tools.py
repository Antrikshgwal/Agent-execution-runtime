"""Tool registration and dispatch.

A tool is a registered function. The runtime looks one up by name, validates the
arguments a decision chose, and calls it. Nothing here knows what any particular
tool does, and nothing here branches on a tool's name.
"""

import inspect
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError, create_model

MOCK_CLOUD_URL = os.environ.get("MOCK_CLOUD_URL", "http://localhost:9000")


class ToolArgsError(ValueError):
    """A decision chose arguments the tool's schema rejects."""


@dataclass
class ToolContext:
    """Carries what the runtime knows and the tool needs, kept out of the schema
    the LLM sees. The tool receives it; the model never chooses it."""

    tool_name: str
    idempotency_key: str
    remote: str | None = None

    async def provision(self, **args: Any) -> str:
        """Send the action under this step's idempotency key.

        The key travels with every attempt, including recovery retries, so the
        remote answers a repeat with its stored result.
        """
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{MOCK_CLOUD_URL}/provision",
                json={
                    "tool_name": self.tool_name,
                    "tool_args": args,
                    "idempotency_key": self.idempotency_key,
                },
            )
        body = response.json()
        self.remote = body["status"]
        return body["result"]


@dataclass
class RegisteredTool:
    name: str
    fn: Callable[..., Awaitable[str]]
    args_model: type[BaseModel]
    supports_idempotency_key: bool
    description: str = ""

    def json_schema(self) -> dict[str, Any]:
        return self.args_model.model_json_schema()

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.args_model(**args).model_dump()
        except ValidationError as exc:
            raise ToolArgsError(f"{self.name}: {exc.error_count()} invalid argument(s)") from exc


REGISTRY: dict[str, RegisteredTool] = {}


def _args_model(fn: Callable[..., Any]) -> type[BaseModel]:
    """Derive the argument schema from the signature. The ToolContext parameter
    is the runtime's business, so it stays out of the model."""
    fields: dict[str, Any] = {}
    for name, param in inspect.signature(fn).parameters.items():
        if param.annotation is ToolContext:
            continue
        if param.annotation is inspect.Parameter.empty:
            raise TypeError(f"{fn.__name__}: parameter '{name}' needs a type annotation")
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[name] = (param.annotation, default)
    # Reject unknown arguments. A planner that invents a field should fail here
    # rather than have the runtime drop it and call the tool anyway.
    return create_model(
        f"{fn.__name__}_args",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )


def tool(
    fn: Callable[..., Awaitable[str]] | None = None,
    *,
    supports_idempotency_key: bool = True,
):
    """Register a tool.

    Set supports_idempotency_key=False for a remote that cannot replay a stored
    result. Recovery flags those rows for a human rather than re-sending, because
    a retry could create a second resource.
    """

    def register(target: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
        REGISTRY[target.__name__] = RegisteredTool(
            name=target.__name__,
            fn=target,
            args_model=_args_model(target),
            supports_idempotency_key=supports_idempotency_key,
            description=inspect.getdoc(target) or "",
        )
        return target

    return register(fn) if fn else register


def get(tool_name: str) -> RegisteredTool:
    try:
        return REGISTRY[tool_name]
    except KeyError:
        raise ToolArgsError(f"no tool registered under '{tool_name}'") from None


async def dispatch(
    tool_name: str, tool_args: dict[str, Any], idempotency_key: str
) -> tuple[str, str]:
    """Validate, then call. Returns the tool's result and the remote's verdict."""
    entry = get(tool_name)
    checked = entry.validate(tool_args)
    context = ToolContext(tool_name=tool_name, idempotency_key=idempotency_key)
    result = await entry.fn(context, **checked)
    return result, context.remote or "unknown"


def schemas_for_model() -> list[dict[str, Any]]:
    """What the planner sees: one entry per tool, so it knows what it can call
    and with which arguments."""
    return [
        {
            "name": entry.name,
            "description": entry.description,
            "parameters": entry.json_schema(),
        }
        for entry in REGISTRY.values()
    ]
