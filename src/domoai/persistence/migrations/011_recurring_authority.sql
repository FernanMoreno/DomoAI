ALTER TABLE recurring_schedules
    ADD COLUMN authority_payload TEXT NOT NULL DEFAULT '{}';
