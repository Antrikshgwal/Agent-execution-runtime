create table if not exists runs (
    run_id      text primary key,
    status      text not null default 'running',   -- running | done | failed
    created_at  timestamptz not null default now()
);

create table if not exists side_effects (
    idempotency_key  text primary key,    -- e.g. 'run42:srv-231'
    run_id           text not null references runs(run_id),
    resource_name    text not null,       -- e.g. 'srv-231'
    spec             text not null,       -- e.g. 't3.micro'
    status           text not null,       -- 'intent' | 'confirmed'
    result           text,                -- cloud resource id, null until confirmed
    created_at       timestamptz not null default now(),
    confirmed_at     timestamptz
);
