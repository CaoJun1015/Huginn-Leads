ALTER TABLE leads ADD COLUMN sender_id TEXT;
ALTER TABLE leads ADD COLUMN sender_name TEXT;

UPDATE leads
SET sender_id=(SELECT r.sender_id FROM raw_messages r WHERE r.id=leads.message_id),
    sender_name=(SELECT r.sender_name FROM raw_messages r WHERE r.id=leads.message_id)
WHERE message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_leads_sender_id ON leads(sender_id);
