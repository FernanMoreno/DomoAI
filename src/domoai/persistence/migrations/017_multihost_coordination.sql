-- Phase 3B: additive durable physical-intent and bounded metric history.
CREATE TABLE IF NOT EXISTS physical_intents (
    household_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('prepared', 'acknowledged', 'confirmed', 'rejected', 'unknown')),
    fencing_epoch INTEGER NOT NULL CHECK (fencing_epoch > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (household_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_physical_intents_status
    ON physical_intents (household_id, status, updated_at);

CREATE TABLE IF NOT EXISTS operational_metric_history (
    sample_id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    process_start_time TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    value REAL NOT NULL,
    labels TEXT NOT NULL DEFAULT '{}',
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_metric_history_instance_time
    ON operational_metric_history (instance_id, recorded_at DESC);
