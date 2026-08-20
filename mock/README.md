# Mocks and schema

The two remotes the runtime talks to, plus the database schema. No agent loop,
worker, or recovery logic here.

All commands below run from this `mock/` directory.

## Setup

```bash
pip install -r requirements.txt
```

## 1. Start Postgres

```bash
docker compose up -d
```

Published on host port **5433**, so a natively installed PostgreSQL on 5432 does
not shadow it.

## 2. Load the schema

```bash
psql postgresql://durable:durable@localhost:5433/durable -f schema.sql
```

Creates `runs`, `journal_events`, and `side_effects`.

## 3. Start the two mocks

The cloud, which every tool calls:

```bash
uvicorn mock_cloud:app --port 9000
```

The planner, which decides one step at a time:

```bash
uvicorn mock_llm:app --port 9100
```

`mock_cloud` answers a repeated idempotency key with its stored result.
`mock_llm` counts every decision it is asked for at `GET /calls`, and embeds that
count in the names it chooses, so a step decided twice looks different from a
step replayed.

Leave both running for the whole of a crash test. Restarting `mock_cloud` wipes
the key map and restarting `mock_llm` resets the counter, and either one voids
the result.

## 4. Verify the setup

In another terminal:

```bash
python check_setup.py
```

Prints `SETUP OK` if:

- `runs`, `journal_events`, and `side_effects` all exist in Postgres
- calling `POST /provision` twice with the same `idempotency_key` returns
  `created` then `already_done`, both with the same `result`
