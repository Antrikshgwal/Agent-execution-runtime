import asyncio
import sys

import asyncpg
import httpx

DATABASE_URL = "postgresql://durable:durable@localhost:5433/durable"
MOCK_CLOUD_URL = "http://localhost:9000"


async def check_tables_exist(conn: asyncpg.Connection) -> list[str]:
    failures = []
    for table in ("runs", "journal_events", "side_effects"):
        exists = await conn.fetchval(
            "select exists (select 1 from information_schema.tables where table_name = $1)",
            table,
        )
        if not exists:
            failures.append(f"table '{table}' does not exist")
    return failures


async def check_idempotent_provision() -> list[str]:
    failures = []
    payload = {
        "tool_name": "create_server",
        "tool_args": {"name": "srv-231", "spec": "t3.micro"},
        "idempotency_key": "check_setup:0",
    }

    async with httpx.AsyncClient() as client:
        first = (await client.post(f"{MOCK_CLOUD_URL}/provision", json=payload)).json()
        second = (await client.post(f"{MOCK_CLOUD_URL}/provision", json=payload)).json()

    if first.get("status") != "created":
        failures.append(f"expected first status 'created', got {first.get('status')!r}")
    if second.get("status") != "already_done":
        failures.append(f"expected second status 'already_done', got {second.get('status')!r}")
    if first.get("result") != second.get("result"):
        failures.append(
            f"expected same result, got {first.get('result')!r} and {second.get('result')!r}"
        )

    return failures


async def main() -> None:
    failures = []

    try:
        conn = await asyncpg.connect(DATABASE_URL)
    except Exception as exc:
        print(f"SETUP FAILED: could not connect to Postgres: {exc}")
        sys.exit(1)

    try:
        failures += await check_tables_exist(conn)
    finally:
        await conn.close()

    try:
        failures += await check_idempotent_provision()
    except Exception as exc:
        failures.append(f"could not reach mock cloud service: {exc}")

    if failures:
        print("SETUP FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)

    print("SETUP OK")


if __name__ == "__main__":
    asyncio.run(main())
