-- Floor-aware readers only. Pre-floor executables require operational exclusion.
CREATE TABLE IF NOT EXISTS runtime_contract_floor (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    minimum_reader INTEGER NOT NULL CHECK (minimum_reader > 0),
    checkpoint_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
INSERT OR IGNORE INTO runtime_contract_floor
    (singleton, minimum_reader, checkpoint_id, created_at)
VALUES (1, 2, 'migration-023-runtime-contract-floor', CURRENT_TIMESTAMP);
