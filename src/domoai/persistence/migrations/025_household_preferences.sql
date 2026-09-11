-- Additive durable profile; keep tables intact when rolling back the binary.
CREATE TABLE IF NOT EXISTS household_preference_heads (
    tenant_id TEXT NOT NULL, household_id TEXT NOT NULL,
    generation TEXT NOT NULL, revision INTEGER NOT NULL CHECK (revision >= 0),
    PRIMARY KEY (tenant_id, household_id)
);
CREATE TABLE IF NOT EXISTS household_preferences (
    tenant_id TEXT NOT NULL, household_id TEXT NOT NULL,
    preference_id TEXT NOT NULL, key TEXT NOT NULL,
    target_kind TEXT NOT NULL CHECK (target_kind IN ('device', 'area')),
    target_id TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (tenant_id, household_id, preference_id),
    UNIQUE (tenant_id, household_id, key, target_kind, target_id)
);
CREATE TABLE IF NOT EXISTS household_preference_history (
    tenant_id TEXT NOT NULL, household_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    preference_id TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (tenant_id, household_id, revision)
);
CREATE TABLE IF NOT EXISTS household_preference_requests (
    tenant_id TEXT NOT NULL, household_id TEXT NOT NULL,
    generation TEXT NOT NULL, principal_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
    preference_id TEXT NOT NULL, response TEXT NOT NULL,
    PRIMARY KEY (tenant_id, household_id, generation, principal_id, idempotency_key)
);
