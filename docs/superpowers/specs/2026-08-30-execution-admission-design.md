# Execution Admission and Bundle Ownership Design

**Feature:** B01 — Execution Admission y Ownership de Bundles  
**Spec:** `specs/158-physical-authority-program/blocks/01-execution-admission.md`  
**Status:** Design approved in chat on 2026-08-30; implementation pending

## Problem

The runtime already has a server-owned `ExecutionAdmission` for physical
execution. `PlanExecutor.execute()` invokes it, and scheduler/bundle-owned
paths identify themselves with `aggregate_owner=True`. However, the MCP layer
still performs separate membership checks for `execute_plan`, `schedule_plan`,
`cancel_scheduled_plan` and `reschedule_plan`. The generic cancellation and
rescheduling paths do not invoke `ExecutionAdmission` at all.

This makes bundle ownership depend on the calling path. A future entry point
could omit one of the duplicated checks while still reaching a state-changing
repository operation.

## Goals

- Make one server-owned admission component authoritative for bundle ownership
  across MCP, scheduler, `BundleCommit` and direct application calls.
- Reject generic mutations of bundle members before any scheduled/bundle state
  changes.
- Preserve the existing aggregate-owner boundary: only `Scheduler` and
  `BundleCommitService` may admit member execution/mutation as the aggregate
  owner.
- Preserve existing public error codes and fail-closed behavior.
- Keep predecessor fan-in and approval checks in the same execution admission
  decision.
- Make rejection evidence auditable without exposing credentials or allowing a
  rejected operation to claim or mutate a plan.

## Non-goals

- No new agent-facing protocol or MCP tool.
- No automatic approval, bundle revision, or authority escalation.
- No change to the bundle data model, predecessor list format, or scheduler
  settlement semantics.
- No change to KNX, Home Assistant or other adapters.

## Options considered

### A. Extend `ExecutionAdmission` for all plan operations — recommended

Keep the existing class as the only policy boundary and add an explicit
operation discriminator for execute, schedule, cancel and reschedule. MCP
resolves the plan and calls this boundary; scheduler and `BundleCommitService`
call it with aggregate ownership. The executor continues to enforce the
execution admission immediately before claim.

This removes duplicated policy while preserving the current interfaces and
the existing atomic repository CAS operations.

### B. Keep policy helpers in MCP

Extract the duplicated checks into a helper used by the MCP server. This would
improve local duplication but would not protect direct application callers or
future transports. It leaves physical authority dependent on the MCP calling
path and is rejected.

### C. Put bundle checks in each repository

Make scheduled and bundle repositories reject member mutations directly. This
would protect storage operations but would mix aggregate policy with generic
persistence and would not provide one consistent decision/audit contract for
executor and non-storage calls. It is rejected as the primary boundary.

## Design

### Admission API

`ExecutionAdmission` remains in `src/domoai/application/execution_admission.py`
and gains one operation-aware entry point. The exact implementation name may
follow the existing naming conventions, but its contract is:

```python
class AdmissionOperation(StrEnum):
    EXECUTE = "execute"
    SCHEDULE = "schedule"
    CANCEL = "cancel"
    RESCHEDULE = "reschedule"

async def admit(
    self,
    plan: Plan,
    *,
    operation: AdmissionOperation = AdmissionOperation.EXECUTE,
    aggregate_owner: bool = False,
) -> AdmissionDecision:
    """Validate ownership and execution authority for one plan operation."""
```

The existing `admit(plan, aggregate_owner=...)` callers remain source
compatible through the default `EXECUTE` operation. The operation-specific
decision uses these existing error codes:

| Operation | Generic bundle-member result |
| --- | --- |
| `execute` | `BUNDLE_MEMBER_EXECUTION_FORBIDDEN` |
| `schedule` | `BUNDLE_MEMBER_EXECUTION_FORBIDDEN` |
| `cancel` | `BUNDLE_MEMBER_CANCEL_FORBIDDEN` |
| `reschedule` | `BUNDLE_MEMBER_RESCHEDULE_FORBIDDEN` |

For a non-member, admission returns normally. For a member, `aggregate_owner`
must be true; otherwise it raises the mapped `DomainError` before any
repository mutation. `EXECUTE` additionally performs the existing bundle
approval, predecessor fan-in, and consumed-grant checks. The aggregate-owned
execution path remains the only caller allowed to pass that boundary for a
member.

### Call graph

```text
MCP execute/schedule/cancel/reschedule
              │
              ▼
      ExecutionAdmission
              │
              ├── generic path: aggregate_owner=False → reject member
              └── owner path: aggregate_owner=True → continue
                         ▲
              Scheduler / BundleCommitService
                         │
                         ▼
                    PlanExecutor
```

The MCP server removes its duplicated `is_member()` and
`is_scheduled_member()` policy branches. It still performs input parsing,
principal authorization, digest/window validation, and approval-store
operations that are specific to the MCP request. Those checks are not bundle
ownership policy and remain at their current boundary.

`DomoticsFacade` keeps passing `aggregate_owner=False` by default. Scheduler
and `BundleCommitService` continue to pass `True` only on their internal
aggregate-owned calls. A direct facade or executor caller therefore receives
the same bundle decision as a direct MCP caller.

### Audit and failure ordering

Admission rejection emits one sanitized audit event containing operation,
plan ID, bundle ID when known, error code and a bounded reason. The audit event
is emitted by the admission boundary so all transports have the same evidence
shape. Existing MCP-specific temporal audit records are retained only where
they describe a rejected temporal revision, but must not replace the central
ownership decision.

The ordering is:

```text
resolve stored plan
  → admission decision/audit
  → approval/digest/window checks specific to the operation
  → repository CAS or executor claim
  → adapter write (execute only)
```

No rejected operation may call `Scheduler.cancel`, `Scheduler.reschedule`,
`PlanExecutor.execute`, or an adapter.

### Concurrency and idempotency

The admission check is read-only. State changes continue through the existing
SQLite transactions and compare-and-set repository methods. Tests must prove
that a generic member mutation racing with an aggregate-owned operation cannot
change a scheduled row after the generic admission rejection, and that a
duplicate accepted request remains governed by the repository/executor
idempotency rules.

The implementation is scoped to the supported single-runtime SQLite ownership
model. It must not introduce an in-memory-only lock as a substitute for the
existing durable CAS boundaries.

## Verification plan

The implementation must add or update tests before production code:

1. `ExecutionAdmission` rejects direct `execute` for a bundle member and does
   not call an adapter.
2. The same admission boundary rejects generic `cancel` and `reschedule`, and
   the scheduled row and bundle ledger remain unchanged.
3. Generic `schedule` is also rejected for a bundle member.
4. Scheduler and `BundleCommitService` can admit the same member with
   `aggregate_owner=True` and still require every predecessor to have
   `CONFIRMED_SUCCESS` evidence.
5. A fan-in member is rejected if any predecessor is missing, failed,
   cancelled, unknown or lacks confirmed-success evidence.
6. Concurrent duplicate calls produce one repository/adapter effect and a
   stable decision.
7. Central admission rejection is audited with sanitized bounded payload.
8. MCP contract tests and the composition path cover the direct and
   aggregate-owned routes together.

Architecture checks remain Import Linter plus the existing unit, contract and
composition gates. No real lab fixture needs to change for this block.

## Residual safety boundary

This design closes bundle ownership and execution-path consistency. It does
not make a bundle approval permanent, bypass freshness or dynamic safety, or
qualify physical hardware. Those controls remain independent admission and
execution gates.
