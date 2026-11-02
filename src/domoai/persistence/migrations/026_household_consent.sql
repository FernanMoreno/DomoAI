-- Additive R/control storage. Roll back the binary only; retain this evidence.
CREATE TABLE IF NOT EXISTS consent_control_heads (
    tenant_id TEXT NOT NULL, household_id TEXT NOT NULL,
    generation TEXT NOT NULL, revision INTEGER NOT NULL CHECK (revision >= 0),
    PRIMARY KEY (tenant_id, household_id)
);
CREATE TABLE IF NOT EXISTS consent_issuance_receipts (
    approval_id TEXT PRIMARY KEY REFERENCES approval_grants(approval_id),
    assertion_nonce TEXT UNIQUE,
    tenant_id TEXT NOT NULL, household_id TEXT NOT NULL,
    generation TEXT NOT NULL, revision INTEGER NOT NULL CHECK (revision >= 0),
    receipt_id TEXT NOT NULL UNIQUE, issued_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consent_receipts (
    tenant_id TEXT NOT NULL, household_id TEXT NOT NULL,
    principal_id TEXT NOT NULL, request_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL, response TEXT NOT NULL,
    PRIMARY KEY (tenant_id, household_id, principal_id, request_id)
);
CREATE TABLE IF NOT EXISTS consent_revocations (
    approval_id TEXT PRIMARY KEY REFERENCES approval_grants(approval_id),
    tenant_id TEXT NOT NULL, household_id TEXT NOT NULL,
    revocation_id TEXT NOT NULL UNIQUE, generation TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    principal_id TEXT NOT NULL, request_id TEXT NOT NULL,
    revoked_at TEXT NOT NULL, response TEXT NOT NULL
);
INSERT INTO consent_control_heads (tenant_id, household_id, generation, revision)
SELECT tenant_id, household_id,
    lower(hex(randomblob(4)) || '-' || hex(randomblob(2)) || '-' ||
          hex(randomblob(2)) || '-' || hex(randomblob(2)) || '-' || hex(randomblob(6))), 0 FROM (
    SELECT json_extract(payload, '$.authority.tenant_id') AS tenant_id,
           json_extract(payload, '$.authority.household_id') AS household_id
    FROM approval_grants WHERE json_valid(payload)
    UNION SELECT tenant_id, household_id FROM household_preference_heads
    UNION SELECT tenant_id, household_id FROM connection_ownership
) WHERE typeof(tenant_id) = 'text' AND length(tenant_id) BETWEEN 1 AND 200
    AND typeof(household_id) = 'text' AND length(household_id) BETWEEN 1 AND 200
ON CONFLICT(tenant_id, household_id) DO NOTHING;
INSERT INTO consent_issuance_receipts
    (approval_id, assertion_nonce, tenant_id, household_id, generation, revision,
     receipt_id, issued_at)
SELECT grants.approval_id, json_extract(grants.payload, '$.assertion_nonce'),
       heads.tenant_id, heads.household_id, heads.generation, heads.revision,
       'legacy:' || grants.approval_id, grants.issued_at
FROM approval_grants AS grants JOIN consent_control_heads AS heads
 ON heads.tenant_id = json_extract(grants.payload, '$.authority.tenant_id')
 AND heads.household_id = json_extract(grants.payload, '$.authority.household_id')
WHERE 1=1 ON CONFLICT(approval_id) DO NOTHING;

-- Publish the reader requirement with this schema and its migration ledger entry.
-- A committed 026 must never admit a writer that omits consent bookkeeping.
UPDATE runtime_contract_floor SET minimum_reader = 4 WHERE minimum_reader < 4;

-- The permit and its prepared receipt share one immutable row and caller transaction.
CREATE TABLE IF NOT EXISTS consent_deliveries (
    permit_id TEXT PRIMARY KEY,
    approval_id TEXT NOT NULL UNIQUE REFERENCES approval_grants(approval_id),
    tenant_id TEXT NOT NULL, household_id TEXT NOT NULL,
    request_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL, response TEXT NOT NULL,
    UNIQUE (tenant_id, household_id, request_id),
    UNIQUE (household_id, idempotency_key),
    FOREIGN KEY (household_id, idempotency_key)
        REFERENCES physical_intents(household_id, idempotency_key)
);
