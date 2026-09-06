CREATE TABLE IF NOT EXISTS state_history (
    history_id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    payload TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    source_adapter_id TEXT NOT NULL,
    source_external_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_state_history_household_observed
    ON state_history (household_id, observed_at DESC, received_at DESC);

CREATE INDEX IF NOT EXISTS idx_state_history_household_device_capability
    ON state_history (household_id, device_id, capability, observed_at DESC);
