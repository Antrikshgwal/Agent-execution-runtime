# Durable Provisioning Runtime — Foundation

This step covers only the mock cloud service and the database schema.
No runtime, worker, or provisioning logic yet.

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
# Agent-execution-runtime
