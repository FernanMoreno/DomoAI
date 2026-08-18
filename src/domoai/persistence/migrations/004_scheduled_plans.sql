CREATE TABLE IF NOT EXISTS scheduled_plans (
    plan_id TEXT PRIMARY KEY,
    execute_at TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
