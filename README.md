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
both reach for `tools` and `fencing`, and nothing reaches back.

- `runtime.py` — the loop: claim the run, recover, then step until DONE
- `journal.py` — decisions, replay, and what the planner cost
- `executor.py` — one tool call, intent row first
- `recovery.py` — what a crash left behind, settled on startup
- `fencing.py` — claiming a run, and what happens to a worker that loses it
- `tools.py` — the `@tool` decorator, the registry, and dispatch
- `demo_tools.py` — the three tools the demo agent chooses among
- `planner.py` — client for the planner
- `crashpoints.py` — the seven points `CRASH_AT` can kill the process at
- `crashtest.py` — runs each of those points and asserts on what recovery produced
- `fencetest.py` — presses each guarded write from both sides of a stolen run
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
skips the cleanup a real `kill -9` also skips. `CRASH_SEQ` chooses which step it
dies on, so the kill can land on the first step or partway through a run.

Points: `before_decide`, `after_decide_before_journal`, `after_decide`,
`before_intent`, `after_intent`, `after_call`, `after_confirm`.

`crashtest.py` runs them. Each case kills a process at one point, restarts it,
and checks what recovery produced against the mock cloud's key map, the mock
planner's call counter, and all three tables:

```bash
python crashtest.py                 # every point, crashing on the first step
python crashtest.py --seq 2         # every point, crashing partway through
python crashtest.py after_call      # one point
```

Both mocks must stay up for the whole run. Restarting `mock_cloud` wipes the key
map and restarting `mock_llm` resets the counter, and either one voids the
result.

Two of the seven cases are the proofs the design exists for:

- **`after_call`** kills the process after the remote acted and before the
  confirm committed. Recovery re-sends under the same key, the remote answers
  `already_done`, the resource count does not move, and the stored result is the
  id from before the crash.
- **`after_decide`** kills it after the decision was journaled and before the
  tool ran. The restart replays the decision, and the planner's counter holds
  flat. A counter that ticks up means the runtime paid to re-think a step it had
  already decided, which breaks the central claim even when the final state
  happens to look right.

To watch one by hand instead:

```bash
CRASH_AT=after_decide RUN_ID=demo python runtime.py   # dies with a decision journaled
RUN_ID=demo python runtime.py                          # recovers
```

## Fencing

A worker takes a run by incrementing `runs.epoch`, conditioned on the value it
just read:

```sql
update runs
   set epoch = epoch + 1, owner = $2, claimed_at = now()
 where run_id = $1 and epoch = $3
returning epoch;
```

Postgres serializes two updates on the same row, so of two workers that read the
same epoch exactly one finds its `where` clause still true. The other matches no
rows and learns it lost before writing anything:

```
INFO  claimed  run=twow  epoch=1  owner=host-16676-4956fe
INFO  fenced   run=twow  epoch=0  observed=1  write=claim
```

Every later write to `journal_events` and `side_effects` asks the same question,
by looking at `runs.epoch` rather than at the epoch stamped on the row being
written. The distinction is the whole point: recovery settles rows an earlier
epoch stranded on every restart, so a worker comparing its epoch against the
row's stamp would fence itself against its own crashed predecessor. Crash a run
and restart it, and you can watch the new epoch adopt the old one's work:

```
INFO  claimed    run=demo  epoch=2  owner=host-9488-4e4c61
INFO  confirmed  run=demo  seq=0  key=demo:0  result=i-0000533  remote=already_done
```

A guarded write that matches nothing is not always a lost race, so the log says
which it was. `fenced` means the run moved to another epoch. `stalled` means it
did not, and the row was simply not in the state the write required.

```bash
python fencetest.py
```

It bumps `runs.epoch` by hand to stand in for another worker's claim, then
presses each guarded write from both sides. The new owner must settle what the
old epoch left behind. Nobody else gets to write at all, including before the
planner call that would otherwise be paid for.

Needs Postgres. The mocks can stay down, since nothing there calls a tool.

`runs.owner` and `runs.claimed_at` are written but nothing reads them. As
recorded, `claimed_at` says when the claim happened, not when the owner was last
alive, so deciding a claim is stale enough to steal would need the owner to
heartbeat it. Claiming does not wait on a lease: a crashed worker leaves no
signal that it died, so a restart has to be able to take its own run back, and
fencing is what makes the previous owner harmless if it was only stalled.
