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
