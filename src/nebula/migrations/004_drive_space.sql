-- Google Drive as one source with many roots.
--
-- Everything people share with Nebula's service account (a folder, a single file or a Shared
-- Drive) lands in one Drive collection, keyed by Drive file id, so a file is stored and indexed
-- once however many people or folders bring it in. Each shared folder, file or drive is a root;
-- whoever shared it manages it. Each file is still visible only to the people Drive shares it with.

create table source_roots (
    id             uuid primary key default gen_random_uuid(),
    collection_id  uuid not null references collections (id) on delete cascade,
    external_id    text not null,
    kind           text not null check (kind in ('folder', 'file', 'drive')),
    name           text not null default '',
    managed_by     uuid references people (id),
    shared_by      text not null default '',          -- the sharer as Drive reports it
    added_via      text not null default 'discovery' check (added_via in ('discovery', 'request', 'migration')),
    status         text not null default 'active' check (status in ('active', 'removed', 'lost')),
    status_note    text not null default '',
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    unique (collection_id, external_id)
);
create index source_roots_manager on source_roots (managed_by);

-- Which root brought an item in, the source's own revision marker (so unchanged files aren't
-- downloaded again) and a fingerprint of its sharing (so unchanged sharing isn't re-read).
alter table items add column source_root text;
alter table items add column source_rev text;
alter table items add column acl_fingerprint text;
create index items_source_root on items (collection_id, source_root) where source_root is not null;

-- When every file's sharing was last re-read in full.
alter table collections add column last_acl_sweep_at timestamptz;

insert into collections (slug, name, description, kind, source_config)
values ('drive', 'Google Drive',
        'Folders, files and Shared Drives people share with Nebula. Each file is visible only to the people Drive shares it with.',
        'gdrive', '{"permissions": "mirror", "space": true}')
on conflict (slug) do nothing;

-- Folders people connected one by one (each its own collection) become roots of the Drive source.
-- Team collections (owned by a group or published with collection-level grants) stay as they are.
create temp table legacy_drive on commit drop as
select c.id, c.name, coalesce(c.owner_person, c.created_by) as manager,
       coalesce(c.source_config->>'folder_id', c.source_config->>'drive_id') as root_id,
       case when c.source_config ? 'drive_id' then 'drive' else 'folder' end as root_kind
from collections c
where c.kind = 'gdrive' and c.status = 'active' and c.slug <> 'drive' and c.owner_group is null
  and coalesce(c.source_config->>'permissions', 'mirror') = 'mirror'
  and (c.source_config ? 'folder_id' or c.source_config ? 'drive_id')
  and not exists (select 1 from grants g where g.resource_type = 'collection' and g.resource_id = c.id);

insert into source_roots (collection_id, external_id, kind, name, managed_by, added_via)
select (select id from collections where slug = 'drive'), root_id, root_kind, name, manager, 'migration'
from legacy_drive
on conflict (collection_id, external_id) do nothing;

-- Move their files across, keeping item ids (and so their grants), chunks and datasets.
-- If two connected folders held the same file, one copy moves and the other is retired.
with pick as (
    select distinct on (i.external_id) i.id, l.root_id
    from items i join legacy_drive l on l.id = i.collection_id
    where not exists (select 1 from items x where x.external_id = i.external_id
                      and x.collection_id = (select id from collections where slug = 'drive'))
    order by i.external_id, (i.status = 'active') desc, i.updated_at desc
)
update items i set collection_id = (select id from collections where slug = 'drive'), source_root = pick.root_id
from pick where i.id = pick.id;

update datasets d set collection_id = i.collection_id
from items i where d.item_id = i.id and d.collection_id <> i.collection_id;

update chunks c set collection_id = i.collection_id
from items i where c.source_type = 'item' and c.source_id = i.id and c.collection_id is distinct from i.collection_id;

update chunks c set collection_id = d.collection_id
from datasets d where c.source_type = 'dataset' and c.source_id = d.id and c.collection_id is distinct from d.collection_id;

delete from chunks c using legacy_drive l where c.collection_id = l.id;
update items i set status = 'deleted', updated_at = now() from legacy_drive l where i.collection_id = l.id;
update collections c set status = 'archived', description = 'Moved into the Google Drive source.'
from legacy_drive l where c.id = l.id;
