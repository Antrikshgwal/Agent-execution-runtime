# Agent-execution-runtime

An agent loop that survives crashes and picks up where it left off.

## The problem

An agent decides what to do, then does it. Both halves are expensive to repeat,
and the process can die between them.

Let's say the agent is provisioning infrastructure. It decides to create a server,
sends the request, and the machine running it dies before the response arrives.
On restart it faces a question its own database cannot answer: did that server
get created? Send the request again and you may be paying for two. Assume it
worked and the run carries on as though a server exists that might not.

The call to the model has the same shape. Ask it twice about the same step and
the second answer may differ, because nothing obliges a model to be consistent.
Act on that second answer and every later step builds on a history that never
happened, which is harder to spot than a duplicate server and worse to unpick.

Neither problem yields to trying harder. The gap between acting and recording
what happened cannot be closed, only made survivable.

## The pattern

Write the intention down before acting, and the outcome after:

```
commit an intent row       about to act
act                        call the model, or call the tool
commit a confirm           this is what came back
```

Both external calls go through it. A crash in between leaves a row saying an
attempt was made without saying how it ended, which is the honest record of a
window that cannot be closed. Resolving those rows is the first thing a restart
does.

Every fact needed to resume lives in Postgres, so a restart reads its state back
rather than remembering it.

## What that buys

**An external action never happens twice.** Every tool call carries an
idempotency key built from the run and the step number, like `run42:3`. A restart
re-sends the stranded call under that same key, and the remote recognises it and
hands back the result it stored the first time instead of doing the work again.
One server, whatever the crash did.

**A decided step is never bought from the model twice.** Decisions are committed
before the runtime acts on them, so a restart replays the recorded decision
instead of asking again. The saving is not the money. It is that the agent's
history stays the one that actually happened.

**Nobody has to notice the crash.** A worker refreshes a timestamp while it
works, so a run whose worker died stops looking like one being worked on. A
supervisor watches for that and starts a fresh worker on it:

```bash
python -m agent_runtime.supervisor
```

```
INFO  supervisor    lease=12.0  poll=3.0  heartbeat=3.0
INFO  resuming      run=selfheal  pid=5392
INFO  worker-exited run=selfheal  code=0
```

That was a worker killed mid-provision with nothing else touching it. It finds
runs abandoned before it started too, so bringing one up after an outage drains
whatever is sitting there.

The supervisor is a process, and nothing here restarts it. That job belongs to
whatever already keeps processes alive on your machine.

## Demo

## Try it yourself

Everything runs locally. The cloud provider and the model are both mock services
in [`mock/`](mock/), so nothing is billed and nothing leaves the machine.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1        # PowerShell; use source .venv/bin/activate elsewhere
pip install -e .
```

Keep that shell activated for everything below. Without it, `python` and
`uvicorn` resolve to whatever is on PATH and will not find these dependencies.

Start Postgres and load the tables it needs:

```bash
docker compose up -d
psql postgresql://durable:durable@localhost:5433/durable -f schema.sql
```

It is published on host port **5433**, change it if you want some other port.

Then start the two mocks as described in [`mock/README.md`](mock/README.md) and
run the agent:

```bash
python -m agent_runtime
```

It provisions a server, a database, and a DNS record, then stops.

To watch it survive a crash, kill it at a named point and start it again on the
same run:

```bash
CRASH_AT=after_decide RUN_ID=demo python -m agent_runtime   # dies after deciding a step
RUN_ID=demo python -m agent_runtime                         # picks the run back up
```
`CRASH_AT` names where in a step the process should die. There are seven, one for
each moment a real crash could land:

| `CRASH_AT` | The process dies |
| --- | --- |
| `before_decide` | before the model is asked anything |
| `after_decide_before_journal` | after the model answered, before the answer is written down |
| `after_decide` | after the decision is written down, before the tool call starts |
| `before_intent` | after the arguments are checked, before the call is written down |
| `after_intent` | after the call is written down, before the request goes out |
| `after_call` | after the remote acted, before its result is written down |
| `after_confirm` | after everything is written down |

`CRASH_SEQ` picks which step it lands on, so the kill can hit the first action or
one partway through a run.

The two worth trying are `after_decide`, below, and `after_call`, which dies in
the worst place there is: the server exists and nothing recorded it. Watch the
restart re-send that call and get `already_done` back instead of building a
second server.

The first process dies with a decision recorded and its tool not yet called. The
second replays that decision instead of asking the model again, runs the tool
once, and finishes:

```
INFO  decided    run=demo  seq=0  tool=create_server  args={"name": "srv-1", ...}  llm_calls=1
!!! CRASH_AT=after_decide seq=0 -- os._exit(1)

INFO  replayed   run=demo  seq=0  tool=create_server  args={"name": "srv-1", ...}  llm_calls=1
INFO  confirmed  run=demo  seq=0  key=demo:0  result=i-0000001  remote=created
INFO  done       run=demo  steps=3  llm_calls=4
```

`llm_calls` holds at 1 across the restart, so the model was not asked about step
0 twice. The name is the same one too, which is that fact seen from the other
side: the mock model writes its own call count into every name it picks, so a
step that had been decided a second time would read `srv-2`.

Try `CRASH_AT=after_call` for the other half. The first process dies with the
server already built and nothing recording it, and the restart's `confirmed` line
comes back `remote=already_done` rather than `created`. The resource count does
not move.

## Where to look next

- [`ARCHITECTURE.md`](ARCHITECTURE.md) explains the modules, the three tables,
  one step end to end, what each crash point leaves behind, how a stolen run
  settles, and how to run the test harnesses.
- [`mock/README.md`](mock/README.md) covers the two mock remotes and how to slow them down.
