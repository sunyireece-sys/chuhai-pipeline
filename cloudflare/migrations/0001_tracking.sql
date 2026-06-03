CREATE TABLE IF NOT EXISTS email_tokens (
  token TEXT PRIMARY KEY,
  buyer_id INTEGER NOT NULL,
  company_name TEXT,
  email_addr TEXT,
  campaign_id TEXT,
  product_slug TEXT,
  destination_path TEXT,
  profile_slug TEXT,
  run_id TEXT,
  sent_ts INTEGER,
  expires_at INTEGER,
  first_click_ts INTEGER,
  last_click_ts INTEGER,
  click_count INTEGER NOT NULL DEFAULT 0,
  bot_click_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  metadata_json TEXT,
  created_at INTEGER NOT NULL DEFAULT (unixepoch()),
  updated_at INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS company_attribution (
  session_id TEXT PRIMARY KEY,
  visitor_id TEXT,
  buyer_id INTEGER,
  token TEXT,
  company_name TEXT,
  email_addr TEXT,
  campaign_id TEXT,
  profile_slug TEXT,
  run_id TEXT,
  first_seen_ts INTEGER NOT NULL,
  last_seen_ts INTEGER NOT NULL,
  source TEXT NOT NULL,
  confidence INTEGER NOT NULL DEFAULT 100,
  country TEXT,
  user_agent TEXT,
  metadata_json TEXT,
  FOREIGN KEY (token) REFERENCES email_tokens(token)
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  session_id TEXT NOT NULL,
  visitor_id TEXT,
  buyer_id INTEGER,
  token TEXT,
  campaign_id TEXT,
  profile_slug TEXT,
  run_id TEXT,
  event_type TEXT NOT NULL,
  url TEXT,
  page_path TEXT,
  page_title TEXT,
  referrer TEXT,
  payload_json TEXT,
  ip_prefix TEXT,
  ip_hash TEXT,
  country TEXT,
  colo TEXT,
  user_agent TEXT,
  is_bot INTEGER NOT NULL DEFAULT 0,
  bot_reason TEXT,
  synced INTEGER NOT NULL DEFAULT 0,
  synced_at INTEGER,
  created_at INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_tokens_buyer ON email_tokens(buyer_id, campaign_id);
CREATE INDEX IF NOT EXISTS idx_tokens_campaign ON email_tokens(campaign_id, sent_ts);
CREATE INDEX IF NOT EXISTS idx_tokens_profile ON email_tokens(profile_slug, run_id);
CREATE INDEX IF NOT EXISTS idx_attribution_buyer ON company_attribution(buyer_id, last_seen_ts);
CREATE INDEX IF NOT EXISTS idx_attribution_token ON company_attribution(token);
CREATE INDEX IF NOT EXISTS idx_attribution_profile ON company_attribution(profile_slug, run_id);
CREATE INDEX IF NOT EXISTS idx_events_buyer_ts ON events(buyer_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_session_ts ON events(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(event_type, ts);
CREATE INDEX IF NOT EXISTS idx_events_token_ts ON events(token, ts);
CREATE INDEX IF NOT EXISTS idx_events_profile_ts ON events(profile_slug, run_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_unsynced ON events(synced, id) WHERE synced = 0;
