BEGIN IMMEDIATE;

ALTER TABLE sources ADD COLUMN source_type TEXT NOT NULL DEFAULT 'group'
  CHECK (source_type IN ('group', 'contact'));
ALTER TABLE sources ADD COLUMN peer_id TEXT;
UPDATE sources SET peer_id=group_id WHERE peer_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_identity
  ON sources(account_id, source_type, peer_id);

ALTER TABLE raw_messages ADD COLUMN source_id INTEGER REFERENCES sources(id);
ALTER TABLE raw_messages ADD COLUMN source_type TEXT NOT NULL DEFAULT 'group'
  CHECK (source_type IN ('group', 'contact'));
ALTER TABLE raw_messages ADD COLUMN peer_id TEXT;
UPDATE raw_messages
SET source_id=(
      SELECT s.id FROM sources s
      WHERE s.account_id=raw_messages.account_id
        AND s.source_type='group'
        AND s.peer_id=raw_messages.group_id
    ),
    peer_id=group_id
WHERE source_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_raw_source_id ON raw_messages(source_id, received_at DESC);

CREATE TABLE IF NOT EXISTS templates (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  built_in INTEGER NOT NULL DEFAULT 0 CHECK (built_in IN (0, 1)),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
  current_version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS template_revisions (
  template_id TEXT NOT NULL REFERENCES templates(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  fields_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(template_id, version)
);

INSERT OR IGNORE INTO templates(
  id, name, built_in, status, current_version, created_at, updated_at
) VALUES (
  'supply-demand', '电脑经销商模式', 1, 'active', 1,
  strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
);

INSERT OR IGNORE INTO template_revisions(template_id, version, fields_json, created_at)
VALUES (
  'supply-demand', 1,
  '[{"key":"direction","label":"任务方向","type":"select","required":true,"show_in_list":true,"match":true,"options":[{"value":"purchase","label":"我要采购"},{"value":"sell","label":"我有库存待售"}],"role":"direction"},{"key":"product","label":"商品描述","type":"text","required":true,"show_in_list":true,"match":false,"role":"product"},{"key":"cpu","label":"CPU","type":"text","required":false,"show_in_list":false,"match":true,"role":"cpu"},{"key":"gpu","label":"GPU","type":"text","required":false,"show_in_list":false,"match":true,"role":"gpu"},{"key":"memory_gb","label":"内存下限 GB","type":"number","required":false,"show_in_list":true,"match":true,"role":"memory_gb"},{"key":"storage_tb","label":"硬盘下限 TB","type":"number","required":false,"show_in_list":false,"match":true,"role":"storage_tb"},{"key":"price","label":"预算/价格","type":"number","required":false,"show_in_list":true,"match":true,"role":"price"},{"key":"quantity","label":"数量下限","type":"number","required":false,"show_in_list":true,"match":true,"role":"quantity"},{"key":"quantity_unit","label":"数量单位","type":"select","required":false,"show_in_list":false,"match":false,"options":[{"value":"台","label":"台"},{"value":"个","label":"个"},{"value":"套","label":"套"},{"value":"件","label":"件"}],"role":"quantity_unit"},{"key":"region","label":"交付地区","type":"text","required":false,"show_in_list":true,"match":true,"help":"采购时代表目标收货地，销售时代表可交付地。","role":"region"}]',
  strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
);

COMMIT;
