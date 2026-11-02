-- P0.2: additive durable connection profiles for the connection control plane.
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

CREATE INDEX IF NOT EXISTS idx_connections_connector_id
    ON connections (connector_id);

CREATE INDEX IF NOT EXISTS idx_connections_enabled
    ON connections (enabled, updated_at DESC);
