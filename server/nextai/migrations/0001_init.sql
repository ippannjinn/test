CREATE TABLE users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
  state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'suspended', 'disabled', 'deleted')),
  must_change_password INTEGER NOT NULL DEFAULT 0,
  avatar_file TEXT,
  bio TEXT NOT NULL DEFAULT '',
  ui_prefs TEXT NOT NULL DEFAULT '{}',
  storage_quota_mb INTEGER NOT NULL DEFAULT 5120,
  generation_quota_daily INTEGER NOT NULL DEFAULT 100,
  concurrent_jobs INTEGER NOT NULL DEFAULT 2,
  queue_priority INTEGER NOT NULL DEFAULT 0,
  rate_limit_per_min INTEGER NOT NULL DEFAULT 30,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  password_changed_at REAL,
  last_login_at REAL,
  disabled_at REAL,
  purge_after REAL
);

CREATE TABLE devices (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  prev_token_hash TEXT,
  rotated_at REAL,
  created_at REAL NOT NULL,
  last_used_at REAL,
  expires_at REAL NOT NULL,
  max_expires_at REAL NOT NULL,
  last_ip TEXT,
  user_agent TEXT,
  revoked_at REAL,
  revoke_reason TEXT
);
CREATE INDEX idx_devices_user ON devices(user_id);
CREATE INDEX idx_devices_prev ON devices(prev_token_hash);

CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,
  csrf_token TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'web' CHECK (kind IN ('web', 'admin_app')),
  device_id TEXT REFERENCES devices(id) ON DELETE SET NULL,
  created_at REAL NOT NULL,
  last_seen_at REAL NOT NULL,
  idle_expires_at REAL NOT NULL,
  absolute_expires_at REAL NOT NULL,
  ip TEXT,
  user_agent TEXT,
  revoked_at REAL,
  revoke_reason TEXT
);
CREATE INDEX idx_sessions_user ON sessions(user_id);

CREATE TABLE login_failures (
  key TEXT PRIMARY KEY,
  count INTEGER NOT NULL,
  first_at REAL NOT NULL,
  locked_until REAL NOT NULL DEFAULT 0,
  lock_level INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  actor_id TEXT,
  actor_name TEXT,
  action TEXT NOT NULL,
  target TEXT,
  ip TEXT,
  details TEXT
);
CREATE INDEX idx_audit_ts ON audit_log(ts);

CREATE TABLE conversations (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  archived INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_conv_user ON conversations(user_id, updated_at);

CREATE TABLE messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
  content TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}',
  job_id TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX idx_msg_conv ON messages(conversation_id, created_at);

CREATE TABLE files (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('upload', 'generated', 'avatar', 'workspace')),
  name TEXT NOT NULL,
  mime TEXT NOT NULL,
  size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  rel_path TEXT NOT NULL,
  job_id TEXT,
  meta TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);
CREATE INDEX idx_files_user ON files(user_id, created_at);

CREATE TABLE memories (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  content TEXT NOT NULL,
  embedding BLOB,
  embed_model TEXT,
  source TEXT NOT NULL DEFAULT 'user',
  pinned INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX idx_mem_user ON memories(user_id);

CREATE TABLE jobs (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  conversation_id TEXT,
  created_at REAL NOT NULL,
  started_at REAL,
  finished_at REAL,
  profile TEXT NOT NULL DEFAULT '{}',
  request TEXT NOT NULL DEFAULT '{}',
  result TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  gpu_seconds REAL NOT NULL DEFAULT 0,
  cost_units REAL NOT NULL DEFAULT 0
);
CREATE INDEX idx_jobs_user ON jobs(user_id, created_at);
CREATE INDEX idx_jobs_status ON jobs(status);

CREATE TABLE usage_daily (
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  day TEXT NOT NULL,
  generation_units REAL NOT NULL DEFAULT 0,
  gpu_seconds REAL NOT NULL DEFAULT 0,
  jobs INTEGER NOT NULL DEFAULT 0,
  tokens INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, day)
);

CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at REAL NOT NULL,
  updated_by TEXT
);

CREATE TABLE model_state (
  model_id TEXT PRIMARY KEY,
  enabled INTEGER NOT NULL DEFAULT 1,
  installed INTEGER NOT NULL DEFAULT 0,
  files TEXT NOT NULL DEFAULT '{}',
  bytes INTEGER NOT NULL DEFAULT 0,
  installed_at REAL,
  calibration TEXT NOT NULL DEFAULT '{}',
  custom_spec TEXT
);

CREATE TABLE bench_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,
  model_id TEXT,
  data TEXT NOT NULL
);
