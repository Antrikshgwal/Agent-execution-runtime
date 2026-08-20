# Mock Setup — Durable Provisioning Runtime

Mock cloud service and database schema used to demo the runtime.
No runtime, worker, or provisioning logic here.

All commands below run from this `mock/` directory.

## Setup

```bash
pip install -r requirements.txt
```

## 1. Start Postgres

```bash
docker compose up -d
```

## 2. Load the schema

```bash
psql postgresql://durable:durable@localhost:5432/durable -f schema.sql
```

## 3. Start the mock cloud service

```bash
uvicorn mock_cloud:app --port 9000
```

## 4. Verify the setup

In another terminal:

```bash
python check_setup.py
```

Prints `SETUP OK` if:
- both `runs` and `side_effects` tables exist in Postgres
- calling `POST /provision` twice with the same `idempotency_key` returns
  `created` then `already_done`, both with the same `result`