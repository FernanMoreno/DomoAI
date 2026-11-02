-- P0 EXEC-001: durable ownership for the physical dispatch claim.
ALTER TABLE physical_intents RENAME TO physical_intents_legacy;

CREATE TABLE physical_intents (
    household_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('prepared', 'dispatching', 'acknowledged', 'confirmed', 'rejected', 'unknown')
    ),
    fencing_epoch INTEGER NOT NULL CHECK (fencing_epoch > 0),
    owner_id TEXT,
    execution_attempt_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (household_id, idempotency_key)
);

INSERT INTO physical_intents (
    household_id,
    idempotency_key,
    payload,
    status,
    fencing_epoch,
    owner_id,
    execution_attempt_id,
    created_at,
    updated_at
)
SELECT
    household_id,
    idempotency_key,
    payload,
    status,
    fencing_epoch,
    json_extract(payload, '$.owner_id'),
    json_extract(payload, '$.execution_attempt_id'),
    created_at,
    updated_at
FROM physical_intents_legacy;

DROP TABLE physical_intents_legacy;

CREATE INDEX IF NOT EXISTS idx_physical_intents_status
    ON physical_intents (household_id, status, updated_at);
