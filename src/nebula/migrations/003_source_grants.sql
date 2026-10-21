-- Access mirrored from the source system (Drive sharing), kept apart from grants people make in
-- Nebula. Each sync replaces an item's source grants, so revoking in Drive revokes in Nebula.
alter table grants add column origin text not null default 'nebula' check (origin in ('nebula', 'source'));
create index grants_source on grants (resource_type, resource_id) where origin = 'source';

-- What the source said about who can open the item, as last seen (for explanations and checks).
alter table items add column source_audience jsonb;
