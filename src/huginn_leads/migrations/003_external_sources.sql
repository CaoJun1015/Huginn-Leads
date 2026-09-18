INSERT OR IGNORE INTO templates(
  id,name,built_in,status,current_version,created_at,updated_at
) VALUES (
  'diaohuo-stock','Diaohuo 电脑库存示例',0,'active',1,
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
);

INSERT OR IGNORE INTO template_revisions(template_id,version,fields_json,created_at)
VALUES (
  'diaohuo-stock',1,
  '[{"key":"direction","label":"任务方向","type":"select","required":true,"show_in_list":true,"match":true,"options":[{"value":"purchase","label":"我要采购"},{"value":"sell","label":"我有资源待售"}],"role":"direction"},{"key":"product","label":"商品描述","type":"text","required":true,"show_in_list":true,"match":false,"role":"product"},{"key":"series","label":"系列关键词","type":"text","required":true,"show_in_list":false,"match":false},{"key":"cpu","label":"CPU","type":"text","required":false,"show_in_list":false,"match":true,"role":"cpu"},{"key":"gpu","label":"GPU","type":"text","required":false,"show_in_list":false,"match":true,"role":"gpu"},{"key":"ram","label":"内存","type":"text","required":false,"show_in_list":true,"match":true},{"key":"storage","label":"存储","type":"text","required":false,"show_in_list":true,"match":true},{"key":"available_quantity","label":"当前库存","type":"number","required":false,"show_in_list":true,"match":false},{"key":"screen","label":"屏幕","type":"text","required":false,"show_in_list":false,"match":false}]',
  strftime('%Y-%m-%dT%H:%M:%SZ','now')
);

CREATE TABLE external_sources (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'sqlite' CHECK (kind = 'sqlite'),
  database_path TEXT NOT NULL,
  query_text TEXT NOT NULL,
  key_column TEXT NOT NULL,
  template_id TEXT NOT NULL REFERENCES templates(id),
  template_version INTEGER NOT NULL,
  field_mapping_json TEXT NOT NULL,
  defaults_json TEXT NOT NULL DEFAULT '{}',
  name_fields_json TEXT NOT NULL,
  alias_fields_json TEXT NOT NULL,
  alias_mode TEXT NOT NULL DEFAULT 'any' CHECK (alias_mode IN ('any', 'all')),
  task_source_ids_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
  last_imported_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE external_records (
  id TEXT PRIMARY KEY,
  external_source_id TEXT NOT NULL REFERENCES external_sources(id) ON DELETE CASCADE,
  external_key TEXT NOT NULL,
  template_id TEXT NOT NULL REFERENCES templates(id),
  template_version INTEGER NOT NULL,
  display_name TEXT NOT NULL,
  aliases_json TEXT NOT NULL,
  values_json TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
  first_imported_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  UNIQUE(external_source_id, external_key)
);

CREATE INDEX idx_external_records_source
  ON external_records(external_source_id, active, updated_at DESC);
CREATE INDEX idx_external_records_task ON external_records(task_id);
