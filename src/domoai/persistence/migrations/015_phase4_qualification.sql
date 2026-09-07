CREATE TABLE IF NOT EXISTS commissioning_qualifications (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    authority_payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_commissioning_qualifications_household
    ON commissioning_qualifications (json_extract(payload, '$.authority.household_id'));
