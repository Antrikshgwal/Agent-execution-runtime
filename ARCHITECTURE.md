# Architecture

The runtime executes an agent loop that survives `kill -9` at any point and
resumes without repeating an external action. This document says how the pieces
fit, what each write is guarded by, and what a restart does with the state a
crash left behind.

The loop is four lines:

```
loop:
    decision = LLM(goal, history)      # journaled
    if decision == DONE: break
    result = call_tool(decision)       # journaled
    history.append(decision, result)
```

Two of those lines touch the outside world. The planner call costs money and may
answer differently the second time. The tool call creates a resource. Both go
through one pattern: commit an intent row, act, commit a confirm. Postgres holds
every fact needed to resume, so a restart reads its state back rather than
remembering it.

## Modules

```
src/agent_runtime/    the runtime
tests/                the three proof harnesses
mock/                 the remotes it talks to, and the Postgres they run against
schema.sql            the tables the runtime's state and history live in
```

`pip install -e .` puts an `agent-runtime` command on the path. It calls
`run_cli` in `runtime.py`, the same entry point `python -m agent_runtime`
reaches, so the module a person runs is the one the crash tests kill.

```mermaid
flowchart TB
    rt["runtime<br/>claim, recover, step until DONE"]

    rec["recovery"]
    jr["journal"]
    ex["executor"]

    pl["planner"]
    fn["fencing"]
    tl["tools"]

    llm[/"planner remote"/]
    pg[("Postgres")]
    cloud[/"tool remote"/]

    rt --> rec
    rt --> jr
    rt --> ex
    rt --> fn
    rec --> ex

    jr --> pl
    jr --> fn
    ex --> fn
    ex --> tl

    pl --> llm
    fn -- every guarded write --> pg
    tl -- under an idempotency key --> cloud
```

The graph runs one way. `recovery` reaches for `executor` and `journal`, both
reach for `tools` and `fencing`, and nothing reaches back. The edge marked
`every guarded write` is the one to read twice: `journal` and `executor` never
write a row without asking `fencing` whether this worker still owns the run, and
`fencing` is the only module that answers.

| Module | Holds |
| --- | --- |
| [runtime.py](src/agent_runtime/runtime.py) | the loop, and closing the run out |
| [journal.py](src/agent_runtime/journal.py) | decisions, replay, and what the planner cost |
| [executor.py](src/agent_runtime/executor.py) | one tool call, intent row first |
| [recovery.py](src/agent_runtime/recovery.py) | what a crash left behind |
| [fencing.py](src/agent_runtime/fencing.py) | the claim, and every guarded write's verdict |
| [tools.py](src/agent_runtime/tools.py) | registration, validation, dispatch |
| [demo_tools.py](src/agent_runtime/demo_tools.py) | the three tools the demo agent chooses among |
| [planner.py](src/agent_runtime/planner.py) | the scripted backend and the Gemini one |
| [crashpoints.py](src/agent_runtime/crashpoints.py) | the seven points `CRASH_AT` and `STALL_AT` inject at |
| [config.py](src/agent_runtime/config.py) | settings read from the environment |
| [logs.py](src/agent_runtime/logs.py) | the one-event-per-line output the tests assert on |

Two boundaries keep the runtime general. Nothing in it branches on a tool's
name, and nothing in it reads `tool_args`; both travel as opaque values from the
decision to the registry. The planner sees the goal, the history rebuilt from
the journal, and the JSON schemas the registry derives from tool signatures.

## Tables

Three tables, all in Postgres, defined in [schema.sql](schema.sql):

| Table | One row per | Key columns |
| --- | --- | --- |
| `runs` | run | `status`, `epoch`, `owner` |
| `journal_events` | step | `seq`, `event_type`, `payload`, `status`, `epoch`, `llm_attempts` |
| `side_effects` | tool call | `idempotency_key`, `tool_name`, `tool_args`, `status`, `result`, `epoch` |

`runs.epoch` decides who may write. The `epoch` column on the two child tables
records who last wrote each row, which is history rather than permission.
Neither child column carries a default: an insert that forgot the column would
otherwise land on epoch 0, where no guarded write would ever match it again.

The idempotency key is `<run_id>:<seq>`. An agent may pick the same tool with the
same arguments at two different steps, so the step number is what separates them,
and the journal carries that step number across a crash. Keying on the tool name
or on a resource name would collapse two distinct actions into one.

`status` on both child tables takes three values, and the middle one is the whole
reason the design exists:

| Value | Means |
| --- | --- |
| `intent` | the runtime is about to act, is acting, or died somewhere around it. The outcome is unknown. |
| `confirmed` | the call returned and the runtime stored the result. Finished, and never to be sent again. |
| `flagged` | the outcome is unknown and the runtime cannot safely find out. A human has to. |

`intent` is not a failure state. It is the honest representation of a window that
cannot be closed, and every restart begins by resolving the rows sitting in it.
`flagged` is reached only from the branch below where no safe retry exists.

## One step

Everything durable about the design is in the order of these messages.

```mermaid
sequenceDiagram
    autonumber
    participant L as runtime
    participant J as journal
    participant P as planner
    participant E as executor
    participant D as Postgres
    participant C as tool remote

    L->>J: decide this step
    J->>D: read the journal row for seq
    alt row confirmed
        D-->>J: the decision
        J-->>L: replay it, no planner call
    else no row, or a row at intent
        J->>D: insert journal row at intent
        J->>D: llm_attempts + 1
        J->>D: read confirmed history
        J->>P: goal, history, tool schemas
        P-->>J: tool_name and tool_args, or done
        J->>D: confirm the journal row
        J-->>L: the decision
    end

    L->>E: run this tool call
    E->>E: validate args against the schema
    E->>D: insert side_effects row at intent
    E->>C: dispatch under run_id:seq
    C-->>E: result, created or already_done
    E->>D: confirm the side_effects row
```

Read the two `insert ... at intent` messages against the two that follow them.
`insert journal row at intent` commits before `goal, history, tool schemas` goes
out, and `insert side_effects row at intent` commits before `dispatch under
run_id:seq` does. A crash anywhere in between leaves a row that says an attempt
was made and does not say how it ended, which is exactly the question recovery
is built to answer. Without those rows, a restart cannot tell an action that
never happened from one whose answer was lost.

`llm_attempts + 1` runs before the planner call rather than after it. A crash
during the call still leaves the spend recorded, and a real provider offers
nothing equivalent to read back after a restart.

`validate args against the schema` sits before the intent row exists, so a
decision carrying bad arguments fails where it cannot cause a side effect.

The `alt` branch is replay. A confirmed row is returned as-is and the planner is
never asked, because asking again could return a different plan and every later
step would build on a divergence the journal describes but that never happened.

## Fault injection

`CRASH_AT` names one of seven points to die at, `STALL_AT` names one to sleep at
for `STALL_MS`, and `CRASH_SEQ` picks the step either lands on. Both share the
same points, because they model the two ways a worker stops being useful without
saying so. The kill uses `os._exit(1)`, which skips stack unwinding, `finally`
blocks, buffer flushes, and connection teardown. Every call site sits on the
normal path, so an injected run and a production run execute the same code.

| Crash point | State left behind | What the restart does |
| --- | --- | --- |
| `before_decide` | no journal row | decides fresh, and the run costs what a clean run costs |
| `after_decide_before_journal` | row at intent, answer lost | decides again, one call more than a clean run; nobody acted on the lost answer |
| `after_decide` | decision confirmed, no call | replays the decision, the planner counter holds flat, the tool runs once |
| `before_intent` | decision confirmed, no call | replays, and the tool runs as a first attempt |
| `after_intent` | call row at intent, remote unreached | re-sends under the same key, and the remote answers `created` |
| `after_call` | resource exists, row at intent | re-sends under the same key, the remote answers `already_done`, and no second resource appears |
| `after_confirm` | row confirmed | skips the step, with no call |

Two rows are the proofs the design exists for. `after_decide` is the agent
proof: the planner's own call counter must not move across the restart.
`after_call` is the tool proof: the remote's resource count must not move.
[tests/crashtest.py](tests/crashtest.py) asserts both against the mocks'
counters as well as against all three tables.

## Recovery

Recovery runs once at startup, before any new work, and reads every
`side_effects` row for the run.

```mermaid
flowchart TB
    S["a side_effects row<br/>for this run"] --> Q{"status"}

    Q -->|confirmed| A["settled"]
    Q -->|flagged| B["blocked"]
    Q -->|intent| K{"does the tool support<br/>an idempotency key?"}

    K -->|yes| R["re-send under the original key,<br/>then confirm the result"]
    K -->|no| F["mark the row flagged"]

    R --> A
    F --> B

    A --> AL["the loop skips that step"]
    B --> BL["the run stops for a human"]
```

`intent` is the only branch that does any work, and it means the outcome is
unknown rather than that the call did not happen. `re-send under the original
key` is safe for that reason: the key matches the lost attempt, so the remote
either performs the work for the first time or hands back what it stored.

The `no` edge out of `does the tool support an idempotency key?` is the case
with no safe move. No request can tell a lost response from a lost request, and
a second attempt could create a second resource, so the row is flagged and the
step joins `blocked`. `the run stops for a human` is deliberate: the agent's
next decision would otherwise build on a result nobody holds.

Journaled decisions are not settled here. `journal.decide_step` handles them as
the loop reaches each step, which keeps replay on the same path a first decision
takes.

## Fencing

A crashed worker leaves no signal that it died, so a restart has to be able to
take its own run back. Claiming therefore never waits on a lease, and fencing is
what makes the previous owner harmless if it turns out to have been alive.

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

The harder case is the one below, where the loser does not lose at claim time at
all:

```mermaid
sequenceDiagram
    autonumber
    participant A as worker A
    participant D as Postgres
    participant B as worker B

    A->>D: claim, epoch 0 to 1
    A->>D: journal a decision at epoch 1
    Note over A: stalls, still holding epoch 1

    B->>D: claim, epoch 1 to 2
    B->>D: settles A's stranded row, finishes the run
    Note over B: the run reads as though<br/>one worker did it alone

    Note over A: wakes up
    A->>D: guarded write at epoch 1
    D-->>A: UPDATE 0
    A->>D: read the live epoch back
    D-->>A: 2
    Note over A: fenced, SystemExit
```

Worker A is never told it lost. It finds out at `UPDATE 0`, on the first write
it attempts after waking, which is why the check has to live in the `where`
clause of every write rather than in a status read at the top of the loop. A
worker can be overtaken between a read and the write that follows it, but not
inside one conditional statement:

```sql
and exists (select 1 from runs r where r.run_id = ... and r.epoch = $n)
```

That question is about `runs`, not about the row being written. `settles A's
stranded row` is why: recovery adopts rows an earlier epoch left behind on every
restart, so a worker comparing its own epoch against the row's stamp would fence
itself against its dead predecessor. Each guarded write restamps the row as it
passes, which keeps that column an accurate record of who last touched it.

`read the live epoch back` happens only after a write has already failed. A
worker must never read the epoch to decide whether to write, since it would pick
up the value the winner installed and wave its own stale write through. The read
exists to name the failure: `fenced` when the live epoch differs, `stalled` when
it does not and the row was simply not in the state the write required. Both are
fatal, and they get different names because `stalled` is a bug in the runtime
while `fenced` is a lost race, and whoever reads the log next should not go
hunting the wrong one.

Crash a run and restart it, and the log shows the new epoch adopting the old
one's work:

```
INFO  claimed    run=demo  epoch=2  owner=host-9488-4e4c61
INFO  confirmed  run=demo  seq=0  key=demo:0  result=i-0000533  remote=already_done
```

`runs.owner` and `runs.claimed_at` are written but nothing reads them. As
recorded, `claimed_at` says when the claim happened, not when the owner was last
alive, so deciding that a claim is stale enough to steal would need the owner to
heartbeat it.

## Tools

A tool is an async function registered by the `@tool` decorator. The registry
derives an argument model from the signature, so the schema the planner sees and
the validation the runtime runs come from one source. `dispatch` looks the tool
up by name, validates, and calls it with a `ToolContext`.

`ToolContext` carries the tool name and the idempotency key. It stays out of the
schema the model sees, so the runtime supplies it and the planner cannot choose
it. `extra="forbid"` on the argument model means a planner that invents a field
fails validation, rather than having the field dropped and the tool called
anyway.

The three demo tools in [demo_tools.py](src/agent_runtime/demo_tools.py) take
different arguments on purpose. A planner that guesses argument names gets caught
before an intent row exists, and three real choices make the journal worth
replaying. Importing that module registers them, which is why
[runtime.py](src/agent_runtime/runtime.py) imports it for the side effect.

## Planner backends

`PLANNER` picks one:

**`mock`**, the default. The scripted HTTP stub in [mock/](mock/), free and
deterministic, and what the crash tests use. A test that paid a provider and
picked a different plan each run would prove nothing. Its `/calls` endpoint is
the instrument the agent proof reads, and every decision it returns embeds the
call number in the resource name, so a second call to it shows up in the
transcript.

**`gemini`**, a real model. Needs the extra and a key:

```bash
pip install -e ".[gemini]"
PLANNER=gemini python -m agent_runtime
```

`GEMINI_API_KEY` is read from `.env.local`, which git ignores, and
`GEMINI_MODEL` overrides the default `gemini-3.5-flash`. A real model makes
replay more than a demonstration: ask it twice about the same step and the second
answer may differ.

Nothing in `planner.py` retries. Deciding whether to call or to replay belongs to
the runtime, and a retry hidden in the client would defeat it.

## Configuration

| Variable | Default | Means |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://durable:durable@localhost:5433/durable` | where the three tables live |
| `RUN_ID` | `run1` | which run to claim, and the run half of every idempotency key |
| `GOAL` | `stand up a web service` | fixed for the life of a run, since replay depends on it not changing |
| `PLANNER` | `mock` | `mock` or `gemini` |
| `MAX_STEPS` | 12, in code | guard against a planner that never says DONE |
| `WORKER` | host, pid, and a random suffix | written to `runs.owner`, diagnostic only |
| `CRASH_AT`, `STALL_AT`, `CRASH_SEQ`, `STALL_MS` | unset | fault injection, above |

Planner calls are counted in `journal_events.llm_attempts`. Counting in the
database rather than at the provider means the number survives `kill -9` and
reflects money spent rather than answers received.

## Log

One event per line, machine-greppable, so a crash test can assert on the output
rather than on a debugger. That is why the format is fixed: a timestamp, a level,
the event name, and `key=value` pairs separated by two spaces.

```
2026-08-21T09:14:03.647Z INFO  calling    run=run42  seq=3  key=run42:3  attempt=1
2026-08-21T09:14:03.861Z INFO  confirmed  run=run42  seq=3  key=run42:3  result=i-0000001  remote=created
```

Every event carries `run` except `registry`, which is emitted before a run is
claimed.

| Event | When | Fields beyond `run` |
| --- | --- | --- |
| `registry` | At startup, before the claim | `tools`, `planner` |
| `claimed` | A worker wins a run | `epoch`, `owner` |
| `deciding` | The journal intent row is committed, before the planner call | `seq`, `llm_calls` |
| `decided` | A decision reaches durable storage | `seq`, `tool`, `args`, `llm_calls` |
| `replayed` | A confirmed decision is replayed instead of re-decided | `seq`, `tool`, `args`, `llm_calls` |
| `intent` | The `side_effects` intent row is committed, before the call | `seq`, `key`, `tool` |
| `calling` | Immediately before the tool's request | `seq`, `key`, `attempt` |
| `confirmed` | The confirm write commits | `seq`, `key`, `result`, `remote` |
| `skipped` | Recovery already settled this step's tool call | `seq`, `reason` |
| `flagged` | A tool call is handed to a human | `key`, `reason` |
| `blocked` | The loop reaches a flagged step and stops | `seq`, `reason` |
| `fenced` | A guarded write matched no rows and the run moved on | `epoch`, `observed`, `write` |
| `stalled` | A guarded write matched no rows and the run did not move | `epoch`, `observed`, `write` |
| `guard` | `MAX_STEPS` is reached without a DONE | `reason` |
| `done` | The agent decides DONE | `steps`, `llm_calls` |

`remote` is `created` or `already_done`, which is how the tool proof reads off
the log. Every decision-related event echoes `llm_calls`, so the flat-counter
proof can be read straight from the transcript without querying anything.

`fenced` and `stalled` are the same failure seen from two sides, and they are
named apart on purpose: `fenced` is a lost race and `stalled` is a bug in the
runtime, and whoever reads the log next should not go hunting the wrong one.

Recovery prints a block rather than interleaving with the stream, so what a
restart found is legible at a glance:

```
================ RECOVERY run=run42 epoch=2 ================
  journal: steps=4  confirmed=4  intent=0
  tools:   calls=4  confirmed=3  intent=1  flagged=0
  reconcile key=run42:3 status=intent -> re-sending with same key
2026-08-21T09:20:11.402Z INFO  calling    run=run42  seq=3  key=run42:3  attempt=2
2026-08-21T09:20:11.556Z INFO  confirmed  run=run42  seq=3  key=run42:3  result=i-0000001  remote=already_done
  RECONCILED key=run42:3 result=i-0000001 remote=already_done  (no new resource)
  llm_calls before=4 after=4  (unchanged)
================ RECOVERY COMPLETE  reconciled=1 flagged=0 ================
```

Two lines there carry the result. `remote=already_done` says recovery found the
resource its lost call had created instead of making a second one.
`llm_calls before=4 after=4` says settling the run cost nothing at the planner.

## Tests

Three harnesses under [tests/](tests/), sharing [harness.py](tests/harness.py)
for reading the world back. Each asserts on the runtime's log as well as on the
database, because the log is what a human reads after an incident, so a claim the
log cannot support is a claim the design has not really made.

| Harness | Presses | Needs |
| --- | --- | --- |
| [crashtest.py](tests/crashtest.py) | each crash point, then a restart | Postgres and both mocks |
| [fencetest.py](tests/fencetest.py) | each guarded write, from both sides of a stolen run | Postgres |
| [racetest.py](tests/racetest.py) | two live workers: one claim race, one steal | Postgres and both mocks |

### crashtest.py

Each case kills a process at one point, restarts it, and checks what recovery
produced against the mock cloud's key map, the mock planner's call counter, and
all three tables:

```bash
python tests/crashtest.py                 # every point, crashing on the first step
python tests/crashtest.py --seq 2         # every point, crashing partway through
python tests/crashtest.py after_call      # one point
```

Both mocks must stay up for the whole run. Restarting `mock_cloud` wipes the key
map and restarting `mock_llm` resets the counter, and either one voids the
result.

Two of the seven cases carry the weight:

- **`after_call`** kills the process after the remote acted and before the
  confirm committed. Recovery re-sends under the same key, the remote answers
  `already_done`, the resource count does not move, and the stored result is the
  id from before the crash.
- **`after_decide`** kills it after the decision was journaled and before the
  tool ran. The restart replays the decision, and the planner's counter holds
  flat. A counter that ticks up means the runtime paid to re-think a step it had
  already decided, which breaks the central claim even when the final state
  happens to look right.

### fencetest.py

```bash
python tests/fencetest.py
```

It bumps `runs.epoch` by hand to stand in for another worker's claim, then
presses each guarded write from both sides. The new owner must settle what the
old epoch left behind. Nobody else gets to write at all, including before the
planner call that would otherwise be paid for. The mocks can stay down, since
nothing there calls a tool.

### racetest.py

```bash
python tests/racetest.py           # both cases
python tests/racetest.py steal     # one case
```

The first case starts both workers together and lets them fight over one run.
Where the loser dies is not asserted: if the two reach the compare-and-swap
together, one is fenced on the claim itself, and if process startup skews them
apart, the second reads the newer epoch, takes the run legitimately, and the
first finds out on its next write. Both orderings are correct.

The second case is the steal drawn above, and the reason `STALL_AT` exists. It
reaches a worker that already holds a run and has journaled a decision, at a
named step, every run. By hand:

```bash
STALL_AT=after_decide STALL_MS=20000 RUN_ID=demo python -m agent_runtime &
RUN_ID=demo python -m agent_runtime
```

Both cases end on the same assertion, which is the one worth making: the run has
to look exactly as though a single worker had done it alone. One resource per
distinct key, no duplicate `seq`, no gaps, no `confirmed` row overwritten, and
the planner charged for the work once.

## Invariants

The tests exist to hold these down:

1. A decision reaches the database before the runtime acts on it.
2. An intent row reaches the database before the call goes out.
3. The idempotency key is `<run_id>:<seq>`, and the journal carries it across a crash.
4. Every write to a child table is guarded on `runs.epoch`, and a write that
   matches no rows ends the process.
5. History comes out of the database, not out of memory.
6. Planner spend increments before the call, so a crash mid-call still records it.
7. The runtime never branches on a tool's name and never reads its arguments.
