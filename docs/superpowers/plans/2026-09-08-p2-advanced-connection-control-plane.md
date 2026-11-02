# P2 Advanced Connection Control Plane Implementation Plan

> **For agentic workers:** Execute the tasks in dependency order with test-first checkpoints. The repository contract forbids commits during this session, so task completion is tracked in the Spec Kit task ledger and verified by tests/diff.

**Goal:** Deliver the four P2 connection-control-plane slices: ETS import/review, KNX Secure, Home Assistant OAuth lifecycle and typed repairs.

**Architecture:** Preserve the existing domain → application → runtime/adapters direction. Adapter boundaries parse protocol-specific input and resolve provider credentials only at the last responsible boundary; application services own persistence, rotation and manager delegation. The local management API exposes the new operator workflows without adding vendor-specific MCP tools.

**Tech Stack:** Python 3.12, Pydantic v2, Starlette, httpx, cryptography, xknx, stdlib XML/ZIP, pytest/pytest-asyncio, ruff, mypy, Import Linter, Testcontainers.

**Spec:** `specs/204-p2-advanced-connection-control-plane/spec.md`

## Global Constraints

- All P2 models are strict and schema version `v1`.
- Raw KNX keys, passwords, OAuth tokens and certificates never enter domain models, responses, logs or audit payloads.
- ETS input is bounded; DTD/external entity processing and arbitrary writes are forbidden.
- ETS persistence requires explicit confirmation and exact source digest; writes are atomic.
- Secure KNX setup fails closed and never falls back to ordinary tunnelling.
- DomoAI is an OAuth client only; its provider endpoints are externally configured.
- Only `retry` may execute without an additional confirmation; all other repair changes require confirmation.
- All runtime mutations go through `ConnectionManager`; no route or repair action calls an adapter directly.
- Existing P0/P1 behavior and lab fixtures remain compatible.

## Task groups

### Task 1: P2 contract foundation

1. Add failing strict-model/schema tests for ETS preview, secure profile, OAuth metadata and repair actions.
2. Run those tests and confirm missing symbols fail.
3. Add the smallest domain models and schema export registration.
4. Run focused model/schema tests and regenerate schemas.

### Task 2: ETS importer

1. Add failing tests for XML and `.knxproj` parsing, namespace normalization, bounds, unsupported DPT warnings, deterministic mapping, digest mismatch and confirmation gate.
2. Implement bounded source reading, normalized group-address extraction and mapping proposal generation in `src/domoai/adapters/knx/ets.py`.
3. Implement atomic persist and API preview/persist routes with explicit confirmation.
4. Run unit, security and API tests, then verify no target change on rejected writes.

### Task 3: KNX Secure

1. Add failing tests for secure option validation, secret bundle resolution, IP/Data mode selection, no insecure fallback, rotation rollback and sanitized diagnostics.
2. Implement `KnxSecureCredentialStore`/service and extend `KnxConnectionProvider` plus `XknxTransport` to construct xknx `SecureConfig` only at the adapter boundary.
3. Wire secure settings into runtime/config without persisting raw values and add API rotation/diagnostics routes where needed.
4. Run focused unit/security/integration tests.

### Task 4: Home Assistant OAuth

1. Add failing tests for PKCE/state, callback exchange, invalid/replayed state, refresh token rotation, revocation and long-lived token migration.
2. Implement the injectable OAuth client and encrypted token bundle storage.
3. Extend HA connection credential resolution and wire lifecycle endpoints/runtime service.
4. Run focused contract/security/integration tests and schema export.

### Task 5: Repair Engine

1. Add failing tests for the fixed diagnostic-to-action catalog, confirmation gates, stale digest, retry delegation and configuration rollback.
2. Implement strict repair models and `RepairEngine` delegating only to `ConnectionManager`.
3. Add management API routes and runtime composition wiring.
4. Run unit, contract, security, integration and composition tests.

### Task 6: Cross-cutting closeout

1. Update provider SDK/operator documentation and P2 quickstart.
2. Run all applicable tests, architecture checks and the project composition check with Docker support.
3. Perform the system composition review across persistence, secret rotation, API, runtime lifecycle, retries, concurrent repairs and configuration propagation.
4. Inspect final diff, refresh Graphify, update all Spec Kit task markers and run verification-before-completion.

## Stop condition

Stop after P2 acceptance scenarios, generated schemas, docs and required verification gates pass. Do not add new providers, active-active coordination, an OAuth authorization server, automatic repair loops or unrelated refactors.
