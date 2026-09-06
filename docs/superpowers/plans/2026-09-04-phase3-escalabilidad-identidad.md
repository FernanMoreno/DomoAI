# Phase 3 — Escalabilidad e identidad

**Spec**: [`specs/185-phase3-escalabilidad-identidad/spec.md`](../../specs/185-phase3-escalabilidad-identidad/spec.md)
**Plan**: [`specs/185-phase3-escalabilidad-identidad/plan.md`](../../specs/185-phase3-escalabilidad-identidad/plan.md)
**Execution**: approved by the user; implement all local-safe Phase 3 slices in this workspace.

## Stop condition

The phase closes when every persisted authority record has an identity context,
MCP ACL decisions are target-scoped, aggregate ownership is capability-based,
tokens rotate/revoke durably, encrypted restore is tested, and federation is a
non-actuating authenticated boundary. Active-active, distributed pools and
remote metrics remain a separately gated follow-up because the repository has
no measured requirement or fencing protocol for them.

## Verification order

1. Focused RED tests and implementation by slice.
2. Persistence/contract/composition checks and schema generation.
3. Full suite, static/architecture gates and project composition.
4. System-composition review, Graphify refresh, diff review and final audit.
