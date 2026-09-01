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

CREATE UNIQUE INDEX IF NOT EXISTS approval_grants_assertion_nonce
    ON approval_grants (json_extract(payload, '$.assertion_nonce'))
    WHERE json_extract(payload, '$.assertion_nonce') IS NOT NULL;
