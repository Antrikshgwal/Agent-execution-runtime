# Agent-execution-runtime

A durable agent runtime. It runs an agent loop and survives `kill -9` at any
point, resuming without repeating an external action.

Two lines of the loop touch the outside world:

```
decision = LLM(goal, history)      # journaled, replayed on recovery
result   = call_tool(decision)     # journaled, re-sent under the same key
```

Both follow one pattern: commit an intent row, act, commit a confirm. A crash
between the action and the confirm leaves a row whose outcome is unknown, and
recovery resolves it. Tool calls get re-sent under their original idempotency
key, so the remote answers `already_done` instead of acting twice. Journaled
decisions get replayed, so the planner is never asked about a step it already
decided.

## Layout

The import graph runs one way. `recovery` reaches for `executor` and `journal`,
both reach for `tools`, and nothing reaches back.

- `runtime.py` — the loop: claim the run, recover, then step until DONE
- `journal.py` — decisions, replay, and what the planner cost
- `executor.py` — one tool call, intent row first
- `recovery.py` — what a crash left behind, settled on startup
- `tools.py` — the `@tool` decorator, the registry, and dispatch
- `demo_tools.py` — the three tools the demo agent chooses among
- `planner.py` — client for the planner
- `crashpoints.py` — the seven points `CRASH_AT` can kill the process at
- `config.py` — settings read from the environment
- `logs.py` — the one-event-per-line output the crash tests assert on
- `schema.sql` — the Postgres tables the runtime's state and history live in
- [`mock/`](mock/) — the two mock remotes. See [`mock/README.md`](mock/README.md).

## Run it

```bash
pip install -r requirements.txt
```

Start Postgres and the mock cloud as described in `mock/README.md`, then pick a
planner.

**Scripted planner** (default). Free, deterministic, and what the crash tests
use. Needs `mock_llm` running:

```bash
python runtime.py
```

**Gemini.** Put `GEMINI_API_KEY=...` in `.env.local`, which git ignores:

```bash
PLANNER=gemini python runtime.py
```

`GEMINI_MODEL` overrides the default `gemini-3.5-flash`. A real model makes
replay more than a demonstration: ask it twice about the same step and the
second answer may differ.

Planner calls are counted in `journal_events.llm_attempts`, incremented before
each call. Counting in the database rather than at the provider means the number
survives `kill -9` and reflects money spent rather than answers received.

## Crash tests

`CRASH_AT` hard-kills the process at a named point with `os._exit(1)`, which
skips the cleanup a real `kill -9` also skips.

```bash
CRASH_AT=after_decide RUN_ID=demo python runtime.py   # dies with a decision journaled
RUN_ID=demo python runtime.py                          # recovers
```

Points: `before_decide`, `after_decide_before_journal`, `after_decide`,
`before_intent`, `after_intent`, `after_call`, `after_confirm`.

Two lines in the restart output carry the result:

- `remote=already_done` — recovery found the resource created before the crash
  instead of creating a second one.
- `llm_calls before=N after=N` — the agent replayed its decisions instead of
  paying to re-think them.
