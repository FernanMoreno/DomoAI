CREATE TABLE IF NOT EXISTS bundle_commits (
    id TEXT PRIMARY KEY,
    bundle_digest TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bundle_commits_status
    ON bundle_commits (status);
