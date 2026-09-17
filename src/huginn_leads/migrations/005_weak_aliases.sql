BEGIN IMMEDIATE;

ALTER TABLE external_sources
ADD COLUMN weak_alias_fields_json TEXT NOT NULL DEFAULT '[]';

ALTER TABLE external_records
ADD COLUMN weak_aliases_json TEXT NOT NULL DEFAULT '[]';

COMMIT;
