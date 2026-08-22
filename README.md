# Agent-execution-runtime

An agent loop that survives `kill -9` and picks up where it left off.

## The problem

An agent decides what to do, then does it. Both halves are expensive to repeat,
and the process can die between them.

Say the agent is provisioning infrastructure. It decides to create a server,
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

**Two workers on one run cannot both write.** A worker holds a number it got by
claiming the run, and every write is conditioned on that number still being the
current one. A worker that stalls long enough for another to take over is never
told; it finds out when its next write is refused, which is early enough to have
changed nothing.

Each of the three has a test harness that fails when the guarantee is removed.

## Try it

Everything runs locally. The cloud provider and the model are both mock services
in [`mock/`](mock/), so nothing is billed and nothing leaves the machine.

```bash
pip install -e .
```

Start Postgres and the two mocks as described in
[`mock/README.md`](mock/README.md), then run the agent:

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

The first process dies with a decision recorded and its tool not yet called. The
second replays that decision instead of asking the model again, runs the tool
once, and finishes:

```
INFO  decided   run=demo  seq=0  tool=create_server  args={"name": "srv-182", ...}  llm_calls=1
!!! CRASH_AT=after_decide seq=0 -- os._exit(1)
INFO  replayed  run=demo  seq=0  tool=create_server  args={"name": "srv-182", ...}  llm_calls=1
INFO  done      run=demo  steps=3  llm_calls=4
```

`llm_calls` holds at 1 across the restart, so the model was not asked about step
0 twice. The name it chose is the same one, which is the same fact seen from the
other side: the mock model writes its own call count into every name it picks, so
a step that had been re-decided would say `srv-2` instead.

## Where to look next

- [`ARCHITECTURE.md`](ARCHITECTURE.md) explains the modules, the three tables,
  one step end to end, what each crash point leaves behind, how a stolen run
  settles, and how to run the test harnesses.
- [`mock/README.md`](mock/README.md) covers Postgres and the two mock remotes.
