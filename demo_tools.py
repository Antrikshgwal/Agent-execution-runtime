"""The three tools the demo agent chooses among.

Their signatures differ on purpose. A planner that guesses argument names gets
caught by validation before the runtime writes an intent row, and three real
choices make the journal worth replaying.

All three hit the same mock service. Importing this module registers them.
"""

from tools import ToolContext, tool


@tool
async def create_server(ctx: ToolContext, name: str, spec: str) -> str:
    """Create a virtual server. spec is an instance size such as t3.micro."""
    return await ctx.provision(name=name, spec=spec)


@tool
async def create_database(ctx: ToolContext, name: str, engine: str, size_gb: int) -> str:
    """Create a managed database. engine is postgres or mysql."""
    return await ctx.provision(name=name, engine=engine, size_gb=size_gb)


@tool
async def create_dns_record(
    ctx: ToolContext, hostname: str, record_type: str, target: str
) -> str:
    """Point a hostname at a target. record_type is A or CNAME."""
    return await ctx.provision(hostname=hostname, record_type=record_type, target=target)
