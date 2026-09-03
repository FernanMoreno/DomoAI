# Plan: Temporal Consent and Safe Rescheduling

1. Prove window/digest drift and DST behavior with red tests.
2. Introduce canonical execution-window evidence and schedule revisions.
3. Make rescheduling invalidate old approval and use pending-row CAS.
4. Protect bundle members and scheduler claims with the same evidence.
5. Verify real SQLite restart/DST composition and architecture gates.

Stop condition: no approved physical action may execute at a time not covered by the approval assertion.

## Closure notes (2026-08-29)

Superseded, not implemented from this outline. Already executed end-to-end
via `specs/119-temporal-consent-reschedule/` (18/18 tasks).
`Plan.execution_window`/`schedule_revision` and `Approval.window_digest`/
`schedule_revision` already exist in `src/domoai/domain/models.py` and are
enforced in `PlanService.assert_executable` (verified directly during B02
approval-authority work this same session). This outline is a duplicate
planning artifact left behind uncommitted; no new code written against it.
