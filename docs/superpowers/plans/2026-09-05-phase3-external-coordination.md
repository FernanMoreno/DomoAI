# Phase 3 external coordination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate real etcd ownership/fencing and a PostgreSQL shared control plane without activating unsafe multi-host modes.

**Architecture:** `EtcdHttpLeaseCoordinator` uses the existing provider-neutral contract and etcd v3 JSON APIs over mTLS. `PostgresDatabase` preserves the repository connection boundary while providing the full shared schema and a verified SQLite migration path. Runtime startup selects these providers only when multi-host settings are complete and the adapter advertises fencing support.

**Tech Stack:** Python 3.12, httpx, psycopg 3, PostgreSQL, etcd v3 JSON gateway, SQLite, pytest, Testcontainers, Ruff, mypy, Import Linter.

**Spec:** `specs/192-phase3-external-coordination/spec.md`

## Global Constraints

- Preserve the default SQLite single-writer runtime.
- Do not use Redis or a fake provider for production coordination.
- Require mTLS for external etcd and PostgreSQL connections in multi-host mode.
- Reject missing provider configuration before adapter connection or physical writes.
- Keep active-active disabled.
- Do not commit, push, release or mutate remote resources.

### Task 1: Etcd provider contract

**Files:**
- Create: `src/domoai/application/etcd_coordination.py`
- Modify: `src/domoai/domain/coordination.py`
- Test: `tests/unit/application/test_etcd_coordination.py`

- [x] Add MockTransport tests for CAS acquire, monotonic takeover, wrong owner, stale epoch, keepalive, release and HTTP/TLS failure.
- [x] Implement base64-safe etcd v3 JSON requests for range, txn, lease grant, keepalive and revoke using `httpx.AsyncClient`.
- [x] Make endpoint rotation bounded and fail closed; never return an uncertain token.
- [x] Run the focused etcd provider tests.

### Task 2: PostgreSQL repository backend

**Files:**
- Modify: `pyproject.toml`
- Create: `src/domoai/persistence/postgres.py`
- Create: `src/domoai/persistence/postgres_schema.sql`
- Modify: `src/domoai/persistence/repositories.py`
- Modify: `src/domoai/persistence/coordination.py`
- Test: `tests/unit/persistence/test_postgres_backend.py`
- Test: `tests/composition/test_phase3_external_postgres.py`

- [x] Add `psycopg[binary]` with a bounded major version and lock it.
- [x] Add the complete equivalent schema, JSON path compatibility function and transaction-safe initialization.
- [x] Implement placeholder/transaction adaptation and preserve repository payload/row semantics.
- [x] Add real-container tests that are skipped only when Docker is unavailable.
- [x] Run unit and optional composition tests.

### Task 3: SQLite to PostgreSQL migration

**Files:**
- Create: `src/domoai/persistence/postgres_migration.py`
- Modify: `src/domoai/admin/cli.py`
- Test: `tests/integration/test_sqlite_postgres_migration.py`

- [x] Add a source reader for every schema table and a transactional destination writer.
- [x] Preserve payload bytes, timestamps, authority fields and idempotency keys.
- [x] Return counts and SHA-256 payload digests; abort on mismatch.
- [x] Expose an explicit `domoai-admin migrate-postgres` command requiring source path and DSN.
- [x] Test successful migration and rollback on mismatch with disposable databases.

### Task 4: Runtime provider selection and fencing capability

**Files:**
- Modify: `src/domoai/config/settings.py`
- Modify: `src/domoai/application/runtime_factory.py`
- Modify: `src/domoai/runtime/ports.py`
- Modify: adapter implementations that can transport fencing
- Test: `tests/contract/test_phase3_external_settings.py`
- Test: `tests/composition/test_phase3_external_runtime.py`

- [x] Add etcd endpoint/TLS and PostgreSQL DSN settings as secret-safe fields.
- [x] Construct the real etcd provider when multi-host is enabled and no test provider was injected.
- [x] Use three PostgreSQL connections for runtime, audit and approval repositories.
- [x] Require explicit adapter fencing capability before connect; keep single-writer behavior unchanged.
- [x] Add tests proving incomplete configuration and unfenced adapters fail closed.

### Task 5: Deployment qualification and documentation

**Files:**
- Modify: `deploy/gateway.env.example`
- Create: `deploy/multihost/README.md`
- Modify: `docs/auditoria-fase-3-escalabilidad-identidad.md`
- Modify: `docs/unified-mcp.md`
- Test: `tests/contract/test_multihost_deployment_docs.py`

- [x] Document 3/5-member etcd quorum, PostgreSQL HA/synchronous durability, mTLS, backups and gateway epoch validation.
- [x] Add a non-activating configuration template; do not add a one-node production Compose service.
- [x] Define active-passive qualification gates and explicitly keep active-active blocked.
- [x] Add documentation contract tests.

### Task 6: Verification

- [x] Run focused tests, full pytest, Ruff, mypy, import-linter and `git diff --check`.
- [x] Run `project-composition-check "$(cat .ai/project-name)"`.
- [x] Run optional real etcd/PostgreSQL composition tests when Docker is available.
- [x] Review provider wiring, migration rollback, fencing boundary and final diff.
- [x] Record any residual HIL/infrastructure blocker without claiming production qualification.
