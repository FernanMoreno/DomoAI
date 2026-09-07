CREATE TABLE IF NOT EXISTS automation_rules (
    rule_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    consent_payload TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('disabled', 'enabled', 'expired')),
    last_event_id TEXT,
    last_fired_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_automation_rules_status
    ON automation_rules (status, updated_at);
