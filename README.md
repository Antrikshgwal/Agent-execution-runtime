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

- `runtime.py` — the agent loop, the journal, and recovery
- `tools.py` — the `@tool` decorator, the registry, and dispatch
- `demo_tools.py` — the three tools the demo agent chooses among
- `planner.py` — client for the planner
- [`mock/`](mock/) — the two mock remotes and the Postgres schema. See
  [`mock/README.md`](mock/README.md).

## Run it

Start Postgres and both mocks as described in `mock/README.md`, then:

```bash
python runtime.py
```

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
