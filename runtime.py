import asyncio
import time

import asyncpg
import httpx

DATABASE_URL = "postgresql://durable:durable@localhost:5433/durable"
MOCK_CLOUD_URL = "http://localhost:9000"


def log(step: str, **fields: str) -> None:
    ts = time.strftime("%H:%M:%S")
    kv = "  ".join(f"{key}={value}" for key, value in fields.items())
    print(f"{ts} [main] {step:<11}{kv}")


async def provision_one(run_id: str, resource_name: str, spec: str) -> None:
    #  build the idempotency key
    idempotency_key = f"{run_id}:{resource_name}"

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # intent write — happens BEFORE the cloud call
        await conn.execute(
            """
            insert into side_effects (idempotency_key, run_id, resource_name, spec, status)
            values ($1, $2, $3, $4, 'intent')
            """,
            idempotency_key,
            run_id,
            resource_name,
            spec,
        )
        log("intent", task=resource_name, spec=spec)

        #  call the mock cloud
        log("calling", task=resource_name)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{MOCK_CLOUD_URL}/provision",
                json={
                    "resource_name": resource_name,
                    "spec": spec,
                    "idempotency_key": idempotency_key,
                },
            )
        result = response.json()["result"]

        #  confirm write — happens AFTER the cloud call returns
        await conn.execute(
            """
            update side_effects
            set status = 'confirmed', result = $1, confirmed_at = now()
            where idempotency_key = $2
            """,
            result,
            idempotency_key,
        )
        log("confirmed", task=resource_name, result=result)
    finally:
        await conn.close()


async def main() -> None:
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(
            "insert into runs (run_id, status) values ($1, 'running')",
            "run1",
        )
    finally:
        await conn.close()

    await provision_one("run1", "srv-231", "t3.micro")

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        row = await conn.fetchrow(
            "select * from side_effects where idempotency_key = $1",
            "run1:srv-231",
        )
    finally:
        await conn.close()

    print(dict(row))


if __name__ == "__main__":
    asyncio.run(main())
