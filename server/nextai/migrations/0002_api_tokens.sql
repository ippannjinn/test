ALTER TABLE users ADD COLUMN is_agent INTEGER NOT NULL DEFAULT 0;

CREATE TABLE api_tokens (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  scopes TEXT NOT NULL,
  created_at REAL NOT NULL,
  created_by TEXT,
  expires_at REAL NOT NULL,
  last_used_at REAL,
  last_ip TEXT,
  revoked_at REAL,
  revoke_reason TEXT
);
CREATE INDEX idx_api_tokens_user ON api_tokens(user_id);
