-- EVT-001: exclusive, recoverable claims for critical-audit delivery.
ALTER TABLE audit_outbox ADD COLUMN lease_owner TEXT;
ALTER TABLE audit_outbox ADD COLUMN lease_id TEXT;
ALTER TABLE audit_outbox ADD COLUMN lease_expires_at TEXT;

CREATE INDEX IF NOT EXISTS idx_audit_outbox_claim
    ON audit_outbox (status, lease_expires_at, sequence);
