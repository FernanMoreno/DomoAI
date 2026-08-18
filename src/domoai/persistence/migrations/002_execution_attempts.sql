CREATE TABLE IF NOT EXISTS execution_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    completed_at TEXT NOT NULL
);
