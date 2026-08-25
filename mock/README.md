# Mocks

The two remotes the runtime talks to. No agent loop, worker, or recovery logic
here, and no Postgres either: the database is the runtime's own, and it is
started from the repository root.

All commands below run from this `mock/` directory, with the virtual environment
activated. Both matter. `uvicorn` resolves the module you name from the working
directory, so `mock_cloud:app` is only importable from here, and an unactivated
shell gets whatever `python` is on PATH, which will not have these dependencies.

## Setup

Create the environment once, from the repository root:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1        # PowerShell
```

```bash
python -m venv .venv
source .venv/bin/activate         # bash, zsh
```

Then install what the mocks need, from here:

```bash
pip install -r requirements.txt
```

The runtime's own dependencies are separate, and installed from the repository
root with `pip install -e .`. Running the full demo wants both.

Postgres has to be up before any of this is worth checking. Bringing it up and
loading `schema.sql` is covered in the [root README](../README.md#try-it-yourself),
because the tables belong to the runtime rather than to these mocks.

## 1. Start the two mocks

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

### Slowing them down

Both answer instantly, which makes a whole run take about four seconds and
leaves nothing to catch if you want to kill the agent by hand. `MOCK_DELAY`
holds every response open for that many seconds:

```powershell
$env:MOCK_DELAY="5"
uvicorn mock_cloud:app --port 9000
```

Set it on each mock you want slowed, before starting it. The delay lands *after*
the work is done, so an agent killed mid-call leaves a resource that exists and a
database that never learned its id. That is the case worth reproducing; delaying
first would only ever give you a call that never happened.

It defaults to zero, and leave it there for the test suites. They spawn dozens of
processes making seven remote calls each, so five seconds a call turns a
three-minute run into half an hour.

Leave both running for the whole of a crash test. Restarting `mock_cloud` wipes
the key map and restarting `mock_llm` resets the counter, and either one voids
the result.

## 2. Verify the setup

In another terminal, activated and in this directory as above:

```bash
python check_setup.py
```

Prints `SETUP OK` if:

- `runs`, `journal_events`, and `side_effects` all exist in Postgres
- `mock_llm` answers `GET /calls` with a count
- calling `POST /provision` twice with the same `idempotency_key` returns
  `created` then `already_done`, both with the same `result`
