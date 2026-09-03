# Identity, State and Readback Persistence Implementation Plan

> Execute with tests first and keep the lab fixtures unchanged.

**Goal:** Preserve stable provider identity through SQLite restart without
rehydrating executable routes, while keeping readback persistence single-owner
and cleanup of removed devices durable.

**Spec:** `specs/158-physical-authority-program/blocks/07-identity-state-persistence.md`

## Tasks

- [x] Add a failing restart/SQLite/rename regression using only a stable
  provider `source_device_id`, with no friendly-name or identity-key fallback.
- [x] Persist the stable source-device identity in `SourceRef` and rehydrate
  it into the registry identity index.
- [x] Preserve the non-executable-after-restart boundary; rebuild routes only
  after live discovery and reconcile the old mutable entity ID.
- [x] Keep composed runtime readbacks on the single `StateStore` persistence
  port and retain the legacy sink only for standalone fixtures.
- [x] Verify removal cleanup, schema round-trip and cross-adapter state
  composition against the existing persistence tests.

## Verification record

- Red regression before implementation: renamed entity resolved to a new
  friendly-name canonical ID instead of the persisted device ID.
- Focused registry/persistence suite: **13 passed**.
- Full/composition/architecture verification is recorded after the remaining
  physical-authority blocks are included.

## Residual risk

`source_device_id` is provider identity evidence, not an execution grant.
Routes remain unavailable until current discovery reconstructs them, and
provider identity changes must be treated as a new reconciliation event.
