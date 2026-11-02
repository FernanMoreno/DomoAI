ALTER TABLE connections ADD COLUMN credential_transfer_id TEXT;
-- Operational evidence survives profile replacement/deletion and database copies.
CREATE TABLE IF NOT EXISTS connection_secret_transfers (
    transfer_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    household_id TEXT NOT NULL,
    deployment_id TEXT NOT NULL,
    secret_ref TEXT NOT NULL UNIQUE,
    profile_id TEXT NOT NULL,
    connector_id TEXT NOT NULL,
    unique_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('create', 'reauth')),
    expected_old_ref TEXT,
    envelope_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'adopted', 'discarding', 'discarded')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_connection_secret_transfers_scope
    ON connection_secret_transfers (tenant_id, household_id, deployment_id, profile_id);
UPDATE runtime_contract_floor SET minimum_reader = 3 WHERE minimum_reader < 3;
