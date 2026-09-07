-- Phase 3: additive identity projections. Canonical authority data remains in
-- each JSON payload; these columns make the audit lane queryable and give
-- operators an explicit migration marker without changing old rows.
ALTER TABLE audit_events
    ADD COLUMN authority_payload TEXT NOT NULL DEFAULT '{}';

ALTER TABLE audit_outbox
    ADD COLUMN authority_payload TEXT NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_plans_household
    ON plans (json_extract(payload, '$.authority.household_id'));

CREATE INDEX IF NOT EXISTS idx_bundle_commits_household
    ON bundle_commits (json_extract(payload, '$.authority.household_id'));

CREATE INDEX IF NOT EXISTS idx_scheduled_plans_household
    ON scheduled_plans (json_extract(payload, '$.authority.household_id'));

CREATE INDEX IF NOT EXISTS idx_state_snapshots_household
    ON state_snapshots (json_extract(payload, '$.authority.household_id'));

CREATE INDEX IF NOT EXISTS idx_automation_rules_household
    ON automation_rules (json_extract(payload, '$.authority.household_id'));

CREATE INDEX IF NOT EXISTS idx_approval_grants_household
    ON approval_grants (json_extract(payload, '$.authority.household_id'));
