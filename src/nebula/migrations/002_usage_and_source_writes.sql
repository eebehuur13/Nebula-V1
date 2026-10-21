-- Durable limits shared by API replicas; original files are charged by stored key.
create table usage_counters (
    person_id uuid not null references people(id),
    metric text not null,
    window_start timestamptz not null,
    used bigint not null default 0 check (used >= 0),
    primary key(person_id, metric, window_start)
);
create index usage_counters_expiry on usage_counters(window_start);
create table storage_accounts (
    person_id uuid primary key references people(id),
    used_bytes bigint not null default 0 check (used_bytes >= 0)
);
create table stored_objects (
    file_key text primary key,
    person_id uuid not null references people(id),
    size_bytes bigint not null check(size_bytes >= 0),
    updated_at timestamptz not null default now()
);
create index stored_objects_person on stored_objects(person_id);

-- A preview is bound to its author, target and exact operation. Tokens are
-- single-use; uncertain remote outcomes are never automatically replayed.
create table source_writes (
    id uuid primary key default gen_random_uuid(),
    person_id uuid not null references people(id),
    collection_id uuid not null references collections(id),
    external_id text not null,
    operation text not null,
    request jsonb not null,
    source_version text not null,
    preview jsonb not null,
    status text not null default 'preview' check(status in ('preview','applying','done','failed','uncertain')),
    result jsonb,
    error text,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null default now() + interval '15 minutes',
    applied_at timestamptz
);
create index source_writes_person on source_writes(person_id, created_at);
