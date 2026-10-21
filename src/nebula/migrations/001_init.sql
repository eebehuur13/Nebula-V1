-- Nebula schema, version 1.
-- Everything Nebula owns lives here: people and groups, collections and what they
-- contain (items, pages, records, datasets), the search index, skills, jobs and audit.

create extension if not exists vector;
create extension if not exists pg_trgm;

-- People, groups, roles ---------------------------------------------------------

create table people (
    id            uuid primary key default gen_random_uuid(),
    email         text not null unique,
    name          text not null default '',
    google_sub    text unique,
    status        text not null default 'active' check (status in ('active', 'deactivated')),
    is_admin      boolean not null default false,
    created_at    timestamptz not null default now(),
    last_seen_at  timestamptz
);

-- Group ids are short slugs. "all_rl" is everyone with an active account; its
-- membership is implicit and never stored in `memberships`.
create table groups (
    id                  text primary key check (id ~ '^[a-z0-9_]{2,40}$'),
    name                text not null,
    description         text not null default '',
    google_group_email  text,
    created_at          timestamptz not null default now()
);
insert into groups (id, name, description)
values ('all_rl', 'All RL', 'Everyone at Rocket Learning with an active Nebula account');

create table memberships (
    group_id   text not null references groups (id) on delete cascade,
    person_id  uuid not null references people (id) on delete cascade,
    role       text not null default 'member' check (role in ('member', 'lead')),
    added_at   timestamptz not null default now(),
    primary key (group_id, person_id)
);
create index memberships_person on memberships (person_id);

-- Positions such as "Programme lead, Andhra Pradesh". Appointments keep the history,
-- so "who led the programme in March?" has an answer.
create table roles (
    id          text primary key check (id ~ '^[a-z0-9_]{2,60}$'),
    name        text not null,
    group_id    text references groups (id) on delete set null,
    is_lead     boolean not null default false,
    created_at  timestamptz not null default now()
);

create table appointments (
    id          uuid primary key default gen_random_uuid(),
    role_id     text not null references roles (id) on delete cascade,
    person_id   uuid not null references people (id) on delete cascade,
    starts_on   date not null default current_date,
    ends_on     date,
    created_by  uuid references people (id),
    created_at  timestamptz not null default now()
);
create index appointments_role on appointments (role_id);

-- Collections: a registered source root, or a notebook where saved knowledge lives --

create table collections (
    id                 uuid primary key default gen_random_uuid(),
    slug               text not null unique check (slug ~ '^[a-z0-9-]{2,60}$'),
    name               text not null,
    description        text not null default '',
    kind               text not null check (kind in ('notebook', 'gdrive', 'notion', 'granola')),
    source_config      jsonb not null default '{}',
    owner_group        text references groups (id),
    owner_person       uuid references people (id),
    status             text not null default 'active' check (status in ('active', 'paused', 'archived')),
    review_days        int not null default 90,
    sync_cursor        text,
    last_synced_at     timestamptz,
    last_full_sync_at  timestamptz,
    sync_error         text,
    audience_check     jsonb not null default '{}',
    created_by         uuid references people (id),
    created_at         timestamptz not null default now()
);

-- Who may do what. A resource's own grants add to what its collection grants.
create table grants (
    resource_type   text not null check (resource_type in ('collection', 'item', 'page', 'record', 'dataset', 'skill')),
    resource_id     uuid not null,
    principal_type  text not null check (principal_type in ('person', 'group')),
    principal_id    text not null,
    level           text not null check (level in ('view', 'edit', 'manage')),
    granted_by      uuid references people (id),
    granted_at      timestamptz not null default now(),
    primary key (resource_type, resource_id, principal_type, principal_id)
);
create index grants_principal on grants (principal_type, principal_id);

-- Sources -----------------------------------------------------------------------

create table items (
    id                  uuid primary key default gen_random_uuid(),
    collection_id       uuid not null references collections (id) on delete cascade,
    external_id         text not null,
    kind                text not null,
    title               text not null default '',
    url                 text not null default '',
    mime_type           text not null default '',
    filename            text not null default '',
    path                text[] not null default '{}',
    author              text not null default '',
    source_created_at   timestamptz,
    source_modified_at  timestamptz,
    content_hash        text,
    version             int not null default 0,
    file_key            text,
    document            jsonb,
    summary             text not null default '',
    status              text not null default 'active'
                        check (status in ('active', 'deleted', 'excluded', 'error', 'pending')),
    status_note         text not null default '',
    created_by          uuid references people (id),
    indexed_at          timestamptz,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    unique (collection_id, external_id)
);
create index items_collection on items (collection_id, status);

create table item_versions (
    item_id             uuid not null references items (id) on delete cascade,
    version             int not null,
    content_hash        text,
    file_key            text,
    title               text not null default '',
    source_modified_at  timestamptz,
    created_at          timestamptz not null default now(),
    primary key (item_id, version)
);

-- Pages: one readable page per thing that matters, written and kept by Claude ----

create table pages (
    id             uuid primary key default gen_random_uuid(),
    collection_id  uuid not null references collections (id) on delete cascade,
    slug           text not null check (slug ~ '^[a-z0-9-]{1,80}$'),
    title          text not null,
    kind           text not null default 'topic'
                   check (kind in ('programme', 'state', 'partner', 'team', 'process', 'metric',
                                   'glossary', 'person', 'topic', 'handover', 'template', 'other')),
    aliases        text[] not null default '{}',
    body           text not null default '',
    status         text not null default 'draft' check (status in ('draft', 'verified', 'archived')),
    owner_group    text references groups (id),
    owner_person   uuid references people (id),
    review_by      date,
    version        int not null default 1,
    created_by     uuid references people (id),
    updated_by     uuid references people (id),
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    unique (collection_id, slug)
);
create index pages_aliases on pages using gin (aliases);
create index pages_title_trgm on pages using gin (lower(title) gin_trgm_ops);

create table page_versions (
    page_id     uuid not null references pages (id) on delete cascade,
    version     int not null,
    title       text not null,
    body        text not null,
    status      text not null,
    edited_by   uuid references people (id),
    note        text not null default '',
    created_at  timestamptz not null default now(),
    primary key (page_id, version)
);

-- Edits to a verified page by someone other than its owner wait here.
create table proposals (
    id            uuid primary key default gen_random_uuid(),
    page_id       uuid not null references pages (id) on delete cascade,
    base_version  int not null,
    title         text,
    body          text not null,
    note          text not null default '',
    proposed_by   uuid references people (id),
    status        text not null default 'open' check (status in ('open', 'accepted', 'rejected')),
    decided_by    uuid references people (id),
    decided_at    timestamptz,
    created_at    timestamptz not null default now()
);

-- Records: decisions, facts, metric definitions, lessons, corrections -----------

create table records (
    id             uuid primary key default gen_random_uuid(),
    collection_id  uuid not null references collections (id) on delete cascade,
    type           text not null check (type in ('decision', 'fact', 'metric', 'lesson', 'correction', 'action', 'note')),
    title          text not null,
    body           text not null default '',
    data           jsonb not null default '{}',
    status         text not null default 'draft' check (status in ('draft', 'verified', 'superseded', 'archived')),
    valid_from     date,
    valid_to       date,
    supersedes     uuid references records (id),
    superseded_by  uuid references records (id),
    owner_group    text references groups (id),
    owner_person   uuid references people (id),
    author         uuid references people (id),
    source_note    text not null default '',
    version        int not null default 1,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);
create index records_collection on records (collection_id, type, status);

create table record_versions (
    record_id   uuid not null references records (id) on delete cascade,
    version     int not null,
    title       text not null,
    body        text not null,
    data        jsonb not null,
    status      text not null,
    edited_by   uuid references people (id),
    created_at  timestamptz not null default now(),
    primary key (record_id, version)
);

-- The graph: every link between sources, pages, records, people and datasets ----

create table links (
    from_type   text not null,
    from_id     uuid not null,
    to_type     text not null,
    to_id       uuid not null,
    rel         text not null default 'links_to'
                check (rel in ('links_to', 'cites', 'evidence', 'about', 'supersedes', 'derived_from')),
    anchor      jsonb not null default '{}',
    created_at  timestamptz not null default now(),
    primary key (from_type, from_id, to_type, to_id, rel)
);
create index links_target on links (to_type, to_id);

-- Spreadsheets loaded as real tables (in schema "data") ------------------------

create schema if not exists data;

create table datasets (
    id             uuid primary key default gen_random_uuid(),
    collection_id  uuid not null references collections (id) on delete cascade,
    item_id        uuid references items (id) on delete cascade,
    sheet          text not null default '',
    table_name     text not null unique,
    title          text not null,
    card           text not null default '',
    columns        jsonb not null default '[]',
    row_count      int not null default 0,
    status         text not null default 'active' check (status in ('active', 'deleted')),
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    unique (item_id, sheet)
);

-- Skills -------------------------------------------------------------------------

create table skills (
    id               uuid primary key default gen_random_uuid(),
    name             text not null unique check (name ~ '^[a-z0-9-]{2,64}$'),
    description      text not null,
    status           text not null default 'draft' check (status in ('draft', 'published', 'retired')),
    owner_group      text references groups (id),
    owner_person     uuid references people (id),
    current_version  int not null default 1,
    created_by       uuid references people (id),
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now()
);

create table skill_versions (
    skill_id    uuid not null references skills (id) on delete cascade,
    version     int not null,
    skill_md    text not null,
    files       jsonb not null default '{}',
    note        text not null default '',
    created_by  uuid references people (id),
    created_at  timestamptz not null default now(),
    primary key (skill_id, version)
);

create table skill_feedback (
    id          bigserial primary key,
    skill_id    uuid not null references skills (id) on delete cascade,
    person_id   uuid references people (id),
    comment     text not null,
    created_at  timestamptz not null default now()
);

-- Search index -------------------------------------------------------------------

create table chunks (
    id             bigserial primary key,
    source_type    text not null check (source_type in ('item', 'page', 'record', 'dataset', 'skill')),
    source_id      uuid not null,
    collection_id  uuid,
    seq            int not null default 0,
    kind           text not null default 'text',
    heading        text not null default '',
    text           text not null,
    locator        jsonb not null default '{}',
    embedding      halfvec(1024),
    embed_model    text,
    tsv            tsvector generated always as (to_tsvector('simple', heading || ' ' || text)) stored,
    created_at     timestamptz not null default now()
);
create index chunks_source on chunks (source_type, source_id);
create index chunks_collection on chunks (collection_id);
create index chunks_tsv on chunks using gin (tsv);
create index chunks_embedding on chunks using hnsw (embedding halfvec_cosine_ops);
create index chunks_unembedded on chunks (id) where embedding is null;

-- Files people upload from Claude's sandbox ---------------------------------------

create table uploads (
    id             uuid primary key default gen_random_uuid(),
    person_id      uuid not null references people (id),
    collection_id  uuid not null references collections (id),
    filename       text not null,
    mime_type      text not null default '',
    title          text not null default '',
    file_key       text not null,
    replaces_item  uuid references items (id),
    item_id        uuid references items (id),
    status         text not null default 'pending' check (status in ('pending', 'received', 'done', 'failed', 'expired')),
    created_at     timestamptz not null default now()
);

-- Jobs: the work queue and anything long-running -----------------------------------

create table jobs (
    id            uuid primary key default gen_random_uuid(),
    kind          text not null,
    payload       jsonb not null default '{}',
    status        text not null default 'queued' check (status in ('queued', 'running', 'done', 'failed', 'cancelled')),
    priority      int not null default 100,
    run_after     timestamptz not null default now(),
    attempts      int not null default 0,
    max_attempts  int not null default 5,
    dedupe_key    text,
    person_id     uuid references people (id),
    progress      real,
    message       text not null default '',
    result        jsonb,
    error         text,
    locked_by     text,
    locked_at     timestamptz,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);
create unique index jobs_dedupe on jobs (dedupe_key) where status in ('queued', 'running');
create index jobs_ready on jobs (priority, run_after) where status = 'queued';

-- Audit -----------------------------------------------------------------------------

create table audit_log (
    id             bigserial primary key,
    at             timestamptz not null default now(),
    person_id      uuid references people (id),
    action         text not null,
    resource_type  text,
    resource_id    text,
    detail         jsonb not null default '{}'
);
create index audit_resource on audit_log (resource_type, resource_id);

-- Read-only role used by the `query` tool. It can read the "data" schema and nothing else.
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'nebula_reader') then
        create role nebula_reader nologin;
    end if;
end
$$;
grant usage on schema data to nebula_reader;
alter default privileges in schema data grant select on tables to nebula_reader;
grant nebula_reader to current_user;
