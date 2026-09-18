ALTER TABLE leads ADD COLUMN origin TEXT NOT NULL DEFAULT 'machine'
  CHECK (origin IN ('machine', 'manual'));
ALTER TABLE leads ADD COLUMN platform TEXT;
ALTER TABLE leads ADD COLUMN account_id TEXT;
ALTER TABLE leads ADD COLUMN source_id INTEGER;
ALTER TABLE leads ADD COLUMN source_type TEXT;
ALTER TABLE leads ADD COLUMN peer_id TEXT;
ALTER TABLE leads ADD COLUMN source_name TEXT;
ALTER TABLE leads ADD COLUMN native_message_id TEXT;
ALTER TABLE leads ADD COLUMN message_time TEXT;
ALTER TABLE leads ADD COLUMN received_at TEXT;

UPDATE leads SET origin='manual'
WHERE item_key GLOB 'manual-*'
   OR CASE WHEN json_valid(evidence_json) THEN json_extract(evidence_json, '$.manual') = 1 ELSE 0 END;

-- Earlier replays could hide manual entries; restore only identified manual rows.
UPDATE leads SET superseded_at=NULL, version=version+1
WHERE origin='manual' AND superseded_at IS NOT NULL;

-- Snapshots deliberately outlive raw_messages. Missing historical values stay NULL.
UPDATE leads SET (platform, account_id, source_id, source_type, peer_id,
                  source_name, native_message_id, message_time, received_at) = (
  SELECT r.platform, r.account_id, r.source_id, r.source_type, r.peer_id,
         COALESCE(s.user_alias, s.platform_name, r.peer_id), r.native_message_id,
         r.message_time, r.received_at
  FROM raw_messages r LEFT JOIN sources s ON s.id=r.source_id
  WHERE r.id=leads.message_id
) WHERE message_id IS NOT NULL;

CREATE INDEX idx_leads_source_received ON leads(source_id, received_at);
CREATE INDEX idx_leads_received ON leads(received_at);
