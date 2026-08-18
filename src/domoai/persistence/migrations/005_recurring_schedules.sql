CREATE TABLE IF NOT EXISTS recurring_schedules (
    schedule_id TEXT PRIMARY KEY,
    template_payload TEXT NOT NULL,
    recurrence_payload TEXT NOT NULL,
    next_execute_at TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
