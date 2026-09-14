CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
  account_id TEXT PRIMARY KEY,
  platform TEXT NOT NULL DEFAULT 'qq',
  user_alias TEXT,
  platform_name TEXT,
  connected INTEGER NOT NULL DEFAULT 0 CHECK (connected IN (0, 1)),
  last_seen_at TEXT,
  updated_at TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id TEXT NOT NULL REFERENCES accounts(account_id),
  group_id TEXT NOT NULL,
  user_alias TEXT,
  platform_name TEXT,
  enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
  last_saved_at TEXT,
  error_code TEXT,
  updated_at TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  UNIQUE(account_id, group_id)
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  template_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active','paused','completed','archived')),
  current_revision INTEGER NOT NULL,
  expires_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS task_revisions (
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL,
  template_version TEXT NOT NULL,
  conditions_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(task_id, revision)
);

CREATE TABLE IF NOT EXISTS task_revision_sources (
  task_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  source_id INTEGER NOT NULL REFERENCES sources(id),
  PRIMARY KEY(task_id, revision, source_id),
  FOREIGN KEY(task_id, revision) REFERENCES task_revisions(task_id, revision) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS raw_messages (
  id TEXT PRIMARY KEY,
  platform TEXT NOT NULL,
  account_id TEXT NOT NULL,
  group_id TEXT NOT NULL,
  native_message_id TEXT NOT NULL,
  sender_id TEXT NOT NULL,
  sender_name TEXT,
  message_time TEXT NOT NULL,
  received_at TEXT NOT NULL,
  text TEXT NOT NULL,
  segments_json TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(platform, account_id, group_id, native_message_id)
);

CREATE TABLE IF NOT EXISTS replays (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  revision INTEGER NOT NULL,
  start_at TEXT NOT NULL,
  end_at TEXT NOT NULL,
  status TEXT NOT NULL,
  eligible_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE TABLE IF NOT EXISTS processing_jobs (
  id TEXT PRIMARY KEY,
  message_id TEXT REFERENCES raw_messages(id) ON DELETE SET NULL,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL,
  processor_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','deferred','processing','done','failed','cancelled','source_expired')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL,
  lease_until TEXT,
  error_code TEXT,
  replay_id TEXT REFERENCES replays(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(message_id, task_id, revision, processor_version)
);

CREATE TABLE IF NOT EXISTS evaluations (
  id TEXT PRIMARY KEY,
  message_id TEXT NOT NULL REFERENCES raw_messages(id) ON DELETE CASCADE,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL,
  processor_version TEXT NOT NULL,
  outcome TEXT NOT NULL,
  reason TEXT NOT NULL,
  result_json TEXT NOT NULL,
  processed_at TEXT NOT NULL,
  UNIQUE(message_id, task_id, revision, processor_version)
);

CREATE TABLE IF NOT EXISTS leads (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  message_id TEXT REFERENCES raw_messages(id) ON DELETE SET NULL,
  item_key TEXT NOT NULL,
  revision INTEGER NOT NULL,
  processor_version TEXT NOT NULL,
  direction TEXT NOT NULL,
  extracted_json TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  comparison_json TEXT NOT NULL,
  outcome TEXT NOT NULL,
  reason TEXT NOT NULL,
  manual_status TEXT NOT NULL DEFAULT 'new' CHECK (manual_status IN ('new','contacted','ignored','completed')),
  manual_overrides_json TEXT NOT NULL DEFAULT '{}',
  note TEXT NOT NULL DEFAULT '',
  seen_at TEXT,
  superseded_at TEXT,
  evidence_expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  UNIQUE(task_id, message_id, item_key)
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  csrf_token TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connection_gaps (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diagnostics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  occurred_at TEXT NOT NULL,
  code TEXT NOT NULL,
  account_id TEXT,
  source_id INTEGER,
  count INTEGER NOT NULL DEFAULT 1,
  details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  occurred_at TEXT NOT NULL,
  object_type TEXT NOT NULL,
  object_id TEXT,
  action TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_raw_received ON raw_messages(received_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_raw_source ON raw_messages(account_id, group_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON processing_jobs(status, next_attempt_at, lease_until);
CREATE INDEX IF NOT EXISTS idx_leads_task ON leads(task_id, outcome, manual_status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_leads_message ON leads(message_id);
CREATE INDEX IF NOT EXISTS idx_sources_enabled ON sources(account_id, group_id, enabled);
