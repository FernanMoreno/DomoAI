CREATE OR REPLACE FUNCTION json_extract(payload TEXT, path TEXT)
RETURNS TEXT
LANGUAGE SQL
IMMUTABLE
AS $$
    SELECT CASE
        WHEN payload IS NULL THEN NULL
        ELSE jsonb_extract_path_text(
            payload::jsonb,
            VARIADIC string_to_array(ltrim(path, '$.'), '.')
        )
    END
$$;

CREATE OR REPLACE FUNCTION julianday(value TEXT)
RETURNS DOUBLE PRECISION
LANGUAGE SQL
IMMUTABLE
AS $$
    SELECT EXTRACT(EPOCH FROM value::timestamptz)
$$;

CREATE TABLE IF NOT EXISTS schema_migrations (
    filename TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL,
    unique_id TEXT NOT NULL,
    endpoint_json TEXT NOT NULL,
    options_json TEXT NOT NULL,
    credential_ref TEXT,
    mapping_ref TEXT,
    enabled INTEGER NOT NULL,
    schema_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(connector_id, unique_id)
);

CREATE TABLE IF NOT EXISTS connection_documents (
    document_ref TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    UNIQUE(kind, digest)
);

CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state_snapshots (
    device_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    payload TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (device_id, capability)
);

CREATE TABLE IF NOT EXISTS policies (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS execution_outcomes (
    plan_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    PRIMARY KEY (plan_id, command_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    authority_payload TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS execution_attempts (
    attempt_id BIGSERIAL PRIMARY KEY,
    plan_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    completed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduled_plans (
    plan_id TEXT PRIMARY KEY,
    execute_at TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recurring_schedules (
    schedule_id TEXT PRIMARY KEY,
    template_payload TEXT NOT NULL,
    recurrence_payload TEXT NOT NULL,
    next_execute_at TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    authority_payload TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS bundle_commits (
    id TEXT PRIMARY KEY,
    bundle_digest TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_state_metadata (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_ownership (
    deployment_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    released_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('active', 'released', 'blocked')),
    uncertain INTEGER NOT NULL DEFAULT 0 CHECK (uncertain IN (0, 1))
);

CREATE TABLE IF NOT EXISTS approval_grants (
    approval_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    valid_until TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'consumed', 'expired', 'revoked')),
    consumed_at TEXT
);

CREATE TABLE IF NOT EXISTS approval_reservations (
    approval_id TEXT PRIMARY KEY REFERENCES approval_grants(approval_id),
    reservation_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('reserved', 'committed', 'released')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (reservation_id, approval_id)
);

CREATE TABLE IF NOT EXISTS audit_outbox (
    sequence BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'delivered', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error TEXT,
    delivered_at TEXT,
    authority_payload TEXT NOT NULL DEFAULT '{}',
    lease_owner TEXT,
    lease_id TEXT,
    lease_expires_at TEXT
);

ALTER TABLE audit_outbox ADD COLUMN IF NOT EXISTS lease_owner TEXT;
ALTER TABLE audit_outbox ADD COLUMN IF NOT EXISTS lease_id TEXT;
ALTER TABLE audit_outbox ADD COLUMN IF NOT EXISTS lease_expires_at TEXT;

CREATE TABLE IF NOT EXISTS automation_rules (
    rule_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    consent_payload TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('disabled', 'enabled', 'expired')),
    last_event_id TEXT,
    last_fired_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commissioning_qualifications (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    authority_payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

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

CREATE TABLE IF NOT EXISTS physical_intents (
    household_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('prepared', 'dispatching', 'acknowledged', 'confirmed', 'rejected', 'unknown')
    ),
    fencing_epoch BIGINT NOT NULL CHECK (fencing_epoch > 0),
    owner_id TEXT,
    execution_attempt_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (household_id, idempotency_key)
);

ALTER TABLE physical_intents
    ADD COLUMN IF NOT EXISTS owner_id TEXT,
    ADD COLUMN IF NOT EXISTS execution_attempt_id TEXT;
ALTER TABLE physical_intents DROP CONSTRAINT IF EXISTS physical_intents_status_check;
ALTER TABLE physical_intents ADD CONSTRAINT physical_intents_status_check CHECK (
    status IN ('prepared', 'dispatching', 'acknowledged', 'confirmed', 'rejected', 'unknown')
);

CREATE TABLE IF NOT EXISTS operational_metric_history (
    sample_id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    process_start_time TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    labels TEXT NOT NULL DEFAULT '{}',
    recorded_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS approval_grants_assertion_nonce
    ON approval_grants (json_extract(payload, '$.assertion_nonce'))
    WHERE json_extract(payload, '$.assertion_nonce') IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_bundle_commits_status ON bundle_commits (status);
CREATE INDEX IF NOT EXISTS idx_audit_outbox_pending ON audit_outbox (status, sequence);
CREATE INDEX IF NOT EXISTS idx_audit_outbox_claim
    ON audit_outbox (status, lease_expires_at, sequence);
CREATE INDEX IF NOT EXISTS idx_automation_rules_status ON automation_rules (status, updated_at);
CREATE INDEX IF NOT EXISTS idx_connections_connector_id ON connections (connector_id);
CREATE INDEX IF NOT EXISTS idx_connections_enabled ON connections (enabled, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_connection_documents_kind_digest
    ON connection_documents (kind, digest);
CREATE INDEX IF NOT EXISTS idx_physical_intents_status
    ON physical_intents (household_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_metric_history_instance_time
    ON operational_metric_history (instance_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_state_history_household_observed
    ON state_history (household_id, observed_at DESC, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_state_history_household_device_capability
    ON state_history (household_id, device_id, capability, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_plans_household ON plans (json_extract(payload, '$.authority.household_id'));
CREATE INDEX IF NOT EXISTS idx_bundle_commits_household
    ON bundle_commits (json_extract(payload, '$.authority.household_id'));
CREATE INDEX IF NOT EXISTS idx_scheduled_plans_household
    ON scheduled_plans (json_extract(payload, '$.authority.household_id'));
CREATE INDEX IF NOT EXISTS idx_state_snapshots_household
    ON state_snapshots (json_extract(payload, '$.authority.household_id'));
CREATE INDEX IF NOT EXISTS idx_automation_rules_household
    ON automation_rules (json_extract(payload, '$.authority.household_id'));
CREATE INDEX IF NOT EXISTS idx_approval_grants_household
    ON approval_grants (json_extract(payload, '$.authority.household_id'));
CREATE INDEX IF NOT EXISTS idx_commissioning_qualifications_household
    ON commissioning_qualifications (json_extract(payload, '$.authority.household_id'));

-- A05: no inferred ownership for pre-existing connection profiles.
CREATE TABLE IF NOT EXISTS connection_ownership (
    connection_id TEXT PRIMARY KEY REFERENCES connections(id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    household_id TEXT NOT NULL,
    deployment_id TEXT NOT NULL,
    bound_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_connection_ownership_scope
    ON connection_ownership (tenant_id, household_id, deployment_id);

-- This marker constrains updated readers; it cannot exclude pre-floor binaries.
CREATE TABLE IF NOT EXISTS runtime_contract_floor (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    minimum_reader INTEGER NOT NULL CHECK (minimum_reader > 0),
    checkpoint_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
INSERT INTO runtime_contract_floor
    (singleton, minimum_reader, checkpoint_id, created_at)
VALUES (1, 2, 'postgres-schema-003-runtime-contract-floor', CURRENT_TIMESTAMP)
ON CONFLICT(singleton) DO NOTHING;
ALTER TABLE connections ADD COLUMN IF NOT EXISTS credential_transfer_id TEXT;

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
