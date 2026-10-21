-- What Claude read from parts of a file that text extraction missed (a scanned page, a chart, a
-- picture of a table), saved back so everyone who can open the file can search it. Each entry
-- belongs to one version of the file: a new version starts again, and a part already done for a
-- version isn't redone.
create table item_extractions (
    item_id     uuid not null references items (id) on delete cascade,
    version     int not null,
    part        text not null,
    text        text not null,
    created_by  uuid references people (id),
    created_at  timestamptz not null default now(),
    primary key (item_id, version, part)
);
