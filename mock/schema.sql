create table if not exists runs (
    run_id      text primary key,
    goal        text not null default '',
    status      text not null default 'running',   -- running | done | failed
    created_at  timestamptz not null default now(),
    epoch       integer not null default 0,        -- fencing token, bumped on claim
    owner       text,                              -- worker holding the claim, diagnostic only
    claimed_at  timestamptz
);

-- Append-only record of the agent loop. One row per LLM decision.
-- Written from phase 5 onward; created here so the schema has one home.
create table if not exists journal_events (
    run_id        text not null references runs(run_id),
    seq           integer not null,     -- step number within the run, from 0
    event_type    text not null,        -- decision | observation | done
    payload       jsonb not null,       -- the decision, the observation, or the final answer
    status        text not null,        -- intent | confirmed
    created_at    timestamptz not null default now(),
    confirmed_at  timestamptz,
    epoch         integer not null default 0,
    primary key (run_id, seq)
);

create table if not exists side_effects (
    idempotency_key  text primary key,  -- '<run_id>:<seq>', e.g. 'run42:3'
    run_id           text not null references runs(run_id),
    seq              integer not null,  -- journal step that decided this call
    tool_name        text not null,     -- registered tool to dispatch, opaque to the runtime
    tool_args        jsonb not null,    -- arguments, opaque to the runtime
    status           text not null,     -- intent | confirmed | flagged
    result           text,              -- tool return value, null until confirmed
    created_at       timestamptz not null default now(),
    confirmed_at     timestamptz,
    epoch            integer not null default 0,
    unique (run_id, seq)
);
