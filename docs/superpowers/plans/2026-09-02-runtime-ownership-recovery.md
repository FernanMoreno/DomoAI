# Stale runtime ownership recovery

## Goal

Provide an operator-only recovery path for a gateway that exited without
releasing its durable `runtime_ownership` row. The command must unblock a
stale deployment without permitting two live runtimes to share authority.

## Safety boundary

- Recovery is outside MCP and is never agent-callable.
- The command acquires the same SQLite advisory lock non-blocking before
  changing the ownership row. A live gateway therefore causes a stable refusal.
- The operator must name the deployment and exact recorded owner ID; mismatches
  are rejected without mutation.
- No plans, approvals, leases, devices, credentials or lab assets are changed.
- The next runtime startup still performs normal plan recovery and physical
  control reconciliation; releasing runtime ownership is not physical stop
  evidence.

## TDD sequence

1. Add contract tests for stale-owner release, active-lock refusal, owner
   mismatch and missing database behavior.
2. Implement a repository-level compare-and-release operation and the
   `domoai-admin runtime release-stale-owner` command.
3. Document the operator recovery procedure and its physical-safety boundary.
4. Run focused admin/persistence tests and all repository gates.

## Stop condition

Stop once a verified stale row can be released atomically, a live lock cannot
be released, a subsequent runtime can acquire the deployment, and no tracked
lab configuration or authority payload is modified beyond the explicitly
requested database recovery.

## Verification record (2026-09-02)

- `tests/contract/test_admin_runtime_ownership.py`: 4 passing tests cover exact
  release, live-lock refusal, owner mismatch without mutation and missing DB.
- The real `data/domoai.sqlite3` row for deployment `default` was inspected
  read-only while no gateway process or `8124` listener existed. Its stale
  owner was released with the exact recorded owner ID; no plan, approval,
  lease, credential or lab file was changed by the recovery operation.
- The next gateway startup acquired ownership and served `/healthz` with HTTP
  200. The runtime composition reported `software-qualified` for the explicit
  lab battery binding and `startup_reconciled=True`.
- `software-qualified` intentionally does not make `/readyz` physically ready;
  matching observed hardware/firmware HIL evidence is still absent.
