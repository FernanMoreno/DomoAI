# Multi-host qualification lab v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Exercise the remaining Phase 3 software and disposable-infrastructure failure modes with two labelled DomoAI hosts while preserving lab-only provenance.

**Architecture:** Keep the existing etcd/Patroni/PostgreSQL/HAProxy topology and add two disposable host-agent containers that use the real lease, fencing, physical-intent, outbox and metric-history contracts. A project-scoped Bash controller injects network/process/database faults, runs a local certificate and PostgreSQL backup drill, and emits a sanitized JSONL report. No host ports, production secrets or production evidence are introduced.

**Tech Stack:** Python 3.12, Bash, Docker Compose, Docker Engine, etcd v3.5, Patroni/PostgreSQL 16, HAProxy, psycopg, httpx, OpenSSL CLI, pytest, Ruff, mypy.

**Spec:** `specs/194-multihost-lab-hardening/spec.md`

## Global Constraints

- All disrupted containers must carry `com.domoai.lab=true` and `com.domoai.not-for-production=true`.
- The lab must publish no host ports and must use unique project-scoped cleanup.
- Existing provider-neutral fencing and persistence contracts remain the only authority semantics.
- Every report must contain `qualification_environment=lab`; the production gate must continue rejecting it.
- Faults must be bounded and fail closed; no automatic repair or external endpoint access is allowed.
- Production active-active remains disabled.
- Existing unrelated dirty-worktree changes must be preserved; no commit or history rewrite is permitted.

---

### Task 1: Lab report and host-agent contracts

**Files:**
- Create: `src/domoai/lab/multihost_lab_v2.py`
- Create: `src/domoai/lab/multihost_host.py`
- Create: `tests/contract/test_multihost_lab_v2_contract.py`
- Create: `tests/unit/lab/test_multihost_host.py`

**Interfaces:**
- `LabScenarioResult` and `LabRecoveryReport` validate and serialize the report contract in `specs/194-multihost-lab-hardening/contracts/lab-recovery-report.md`.
- `HostAgent` accepts one JSONL request per connection with `acquire`, `release`, `physical_intent`, `recover`, `outbox_append`, `outbox_dispatch`, `metric_sample` and `status` actions.
- Host responses contain only allowlisted status, epoch, idempotency and metric fields; no token, DSN or certificate material.

- [X] Step 1: Write RED tests for lab provenance, secret-safe diagnostics, malformed actions and idempotent duplicate physical intents.
- [X] Step 2: Run `uv run pytest tests/contract/test_multihost_lab_v2_contract.py tests/unit/lab/test_multihost_host.py -q` and confirm the missing contracts fail.
- [X] Step 3: Implement the pure report models and the JSONL host-agent boundary using the existing coordination, ledger, outbox and metric repositories.
- [X] Step 4: Re-run the focused tests and confirm GREEN; reject unknown actions and secret-shaped fields.

### Task 2: Two-host disposable topology

**Files:**
- Modify: `deploy/multihost/qualification/compose.yaml`
- Modify: `deploy/multihost/qualification/runner/Dockerfile`
- Modify: `tests/contract/test_multihost_qualification_docs.py`
- Create: `tests/contract/test_multihost_lab_v2_topology.py`

**Interfaces:**
- The existing seven-service base topology remains valid.
- The runner image contains the host agent and can be started twice with distinct `DOMOAI_INSTANCE_ID` values.
- The controller can discover one project network and only interrupt labelled containers.

- [X] Step 1: Add RED topology assertions for two host identities, internal-only networking, lab labels and no host ports.
- [X] Step 2: Run the topology tests and observe the missing host contract.
- [X] Step 3: Add explicit host-agent image/entrypoint support without changing production compose files.
- [X] Step 4: Run `docker compose -f deploy/multihost/qualification/compose.yaml config` and topology tests.

### Task 3: Ownership race, partition and fencing scenarios

**Files:**
- Create: `scripts/run_multihost_lab_v2.sh`
- Modify: `src/domoai/lab/multihost_host.py`
- Create: `tests/integration/test_multihost_lab_v2.py`

**Interfaces:**
- `run_multihost_lab_v2.sh` emits JSONL scenario records and exits non-zero on any unsafe/incomplete result.
- The script starts two host containers, runs 20 acquisition races, disconnects/reconnects only a labelled owner, waits for lease expiry, and verifies stale/replay rejection.
- Cleanup removes host containers plus the Compose project and reports cleanup failure.

- [X] Step 1: Add RED Docker integration assertions for two host IDs, one owner, zero unauthorized writes and stale-epoch rejection.
- [X] Step 2: Run the integration test and confirm it fails because the v2 runner is absent.
- [X] Step 3: Implement labelled host startup, JSONL request helper, race collection, network partition, takeover polling and bounded diagnostics.
- [X] Step 4: Run the Docker scenario and fix any real race without weakening the fail-closed assertions.

### Task 4: Crash, outbox and persistence recovery

**Files:**
- Modify: `src/domoai/lab/multihost_host.py`
- Modify: `scripts/run_multihost_lab_v2.sh`
- Create: `tests/integration/test_multihost_lab_v2_recovery.py`

**Interfaces:**
- `physical_intent` accepts a lab-only `crash_after_claim` injection and exits before gateway acknowledgement.
- `recover` marks in-flight intents unknown using the existing repository semantics.
- `outbox_dispatch` records retry and exactly one sink event ID across replay.

- [X] Step 1: Add RED tests for crash-after-claim, restart recovery, outbox retry and duplicate dispatch.
- [X] Step 2: Run the recovery tests and confirm the scenario boundaries are missing.
- [X] Step 3: Implement crash/restart and outbox sink behavior with file locking and shared PostgreSQL state.
- [X] Step 4: Run recovery integration against disposable PostgreSQL and verify no duplicate accepted command.

### Task 5: Control-plane fault matrix

**Files:**
- Modify: `scripts/run_multihost_lab_v2.sh`
- Modify: `tests/integration/test_multihost_lab_v2.py`
- Create: `deploy/multihost/qualification/fault-matrix.md`

**Interfaces:**
- Scenario IDs `control-plane-loss` and `database-primary-failover` report recovery/fail-closed status, elapsed time and bounded diagnostics.
- etcd member loss preserves quorum; PostgreSQL primary stop must observe a different writable Patroni primary.

- [X] Step 1: Add RED assertions for one etcd member loss, database primary interruption and deadline diagnostics.
- [X] Step 2: Implement project-labelled stop/start and health polling using existing primary/replica probes.
- [X] Step 3: Add the operator fault matrix with expected safe outcomes and non-goals.
- [X] Step 4: Run the real Docker matrix and record actual timings.

### Task 6: Ephemeral mTLS profile

**Files:**
- Create: `deploy/multihost/qualification/tls/openssl.cnf`
- Create: `deploy/multihost/qualification/tls/generate.sh`
- Modify: `deploy/multihost/qualification/compose.tls.yaml`
- Modify: `scripts/run_multihost_lab_v2.sh`
- Create: `tests/contract/test_multihost_lab_v2_tls.py`

**Interfaces:**
- `generate.sh` writes only under a mode-0700 temporary lab directory and never reads repository/production keys.
- The secure profile verifies valid client material, rejects an expired client and records certificate generation/rotation metadata without PEM contents.

- [X] Step 1: Add RED contract tests for no production key reads, mode-0700 output and invalid-certificate failure.
- [X] Step 2: Implement ephemeral CA/client generation and a TLS probe profile for the lab endpoints.
- [X] Step 3: Add valid, expired and rotated certificate scenarios with bounded failure output.
- [X] Step 4: Run the secure Docker profile and verify cleanup of certificates.

### Task 7: Backup/restore and bounded load/metrics

**Files:**
- Modify: `scripts/run_multihost_lab_v2.sh`
- Create: `deploy/multihost/qualification/load_generator.py`
- Create: `deploy/multihost/qualification/backup_restore.sh`
- Create: `tests/contract/test_multihost_lab_v2_operations.py`
- Create: `tests/integration/test_multihost_lab_v2_operations.py`

**Interfaces:**
- The backup drill uses `pg_dump`/`pg_restore` into disposable destinations and validates recovery sentinels.
- The load generator uses bounded per-household counts and emits queue/fencing/outbox/metric observations only.

- [X] Step 1: Add RED tests for sentinel preservation, bounded queue admission, metric-history cap and secret-safe output.
- [X] Step 2: Implement PostgreSQL backup/restore and load generation with explicit deadlines.
- [X] Step 3: Add the scenario records and fail the run if any sentinel or bound is violated.
- [X] Step 4: Run the Docker operations profile and verify measured timings plus cleanup.

### Task 8: Documentation and cross-system verification

**Files:**
- Modify: `deploy/multihost/README.md`
- Modify: `docs/auditoria-fase-3-escalabilidad-identidad.md`
- Modify: `specs/193-multihost-production-qualification/tasks.md`
- Modify: `docs/superpowers/plans/2026-09-06-multihost-lab-v2.md`
- Modify: `specs/194-multihost-lab-hardening/tasks.md`

**Interfaces:**
- Documentation separates Docker evidence, production infrastructure evidence and physical HIL.
- The task ledger records each scenario, test command and residual risk.

- [X] Step 1: Add documentation contract assertions for every scenario and limitation.
- [X] Step 2: Update the operator runbook and Phase 3 addendum with real results.
- [X] Step 3: Run focused tests, full pytest, `project-composition-check`, Ruff, mypy, docs/schema checks and final diff review.
- [X] Step 4: Refresh Graphify and run `system-composition-review`; record PASS/PASS WITH RISKS and verify no labelled lab container remains.

## Dependencies and execution order

- Task 1 blocks Tasks 3–7.
- Task 2 blocks Tasks 3–7 and can be completed after Task 1 contracts are defined.
- Tasks 3 and 4 share the host agent and runner script and are sequential.
- Task 5 depends on the base failover logic from Task 3.
- Task 6 is independent of the host protocol after Task 2 but must finish before the final run.
- Task 7 depends on shared PostgreSQL access and host report output.
- Task 8 depends on all scenario implementations and verification.

No commits are planned because the repository operating contract explicitly
forbids committing or rewriting the user's shared worktree.
