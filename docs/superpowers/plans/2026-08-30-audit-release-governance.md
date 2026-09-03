# Audit and Release Governance Implementation Plan

> Execute with tests first and keep the lab fixtures unchanged.

**Goal:** Align durable audit isolation, risk configuration, bounded queries,
CI gates, branch protection and release evidence with the guarantees claimed by
the runtime.

**Spec:** `specs/158-physical-authority-program/blocks/09-audit-release-governance.md`

## Tasks

- [x] Verify the audit lane uses a separate durable database and reject an
  explicit authority/audit path collision.
- [x] Verify ordinary risk overrides are monotonic hardening and cannot lower
  a fail-closed classification through the TOML loader.
- [x] Reject non-positive audit query limits before SQL execution.
- [x] Keep every blocking CI job in `required-release-gates`, including
  performance, schemas, docs, Import Linter and dependency audit.
- [x] Configure `main` branch protection to require all individual gates and
  the aggregate release gate.
- [x] Keep CI evidence SHA-tagged and remove hardcoded test counts from the
  current README claims.

## Verification record

- New storage-isolation regression: **1 passed**.
- Full regression: **1398 passed, 15 skipped, 1 warning** before the B09 test;
  the B09 focused regression and all prior gates pass afterward.
- Mypy, Ruff, Import Linter, architecture, runtime docs and composition fast
  gate: **passed**.
- GitHub API branch-protection review after update: strict checks include
  **15 required contexts**, including `Required release gates`.

## Residual risk

Branch protection is repository configuration outside the source tree and can
be changed by a repository administrator. The workflow aggregate is the
in-repository defense; the live protection check must be re-audited after any
repository-settings change.
