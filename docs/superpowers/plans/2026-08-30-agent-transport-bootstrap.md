# Agent Transport and Runtime Bootstrap Implementation Plan

> Execute with tests first and keep the lab fixtures unchanged.

**Goal:** Make the shared MCP gateway safe for any compatible agent while
keeping local stdio fixtures explicit and exposing a verifiable, non-secret
runtime capability matrix.

**Spec:** `specs/158-physical-authority-program/blocks/08-agent-transport-bootstrap.md`

## Tasks

- [x] Add regressions for strict configured-adapter bootstrap and invalid token
  files before runtime resources are created.
- [x] Keep the deterministic simulator available only through the explicit
  stdio fixture path; make the long-lived gateway fail closed without a real
  provider.
- [x] Validate the token file before runtime construction so invalid launcher
  credentials cannot open SQLite, claim ownership or connect adapters.
- [x] Publish `domotics://runtime` with active providers, writable capability
  routes and sanitized authority status.
- [x] Verify the unified resource catalog and runtime matrix through the MCP
  contract and transport-parity suites.

## Verification record

- Focused bootstrap, gateway, MCP contract and transport-parity suite:
  **53 passed**.
- Full regression: **1398 passed, 15 skipped, 1 warning**.
- Ruff checks for all B08 source and test files: **passed**.
- Mypy, Import Linter, architecture contracts, runtime documentation checks,
  composition fast gate and `git diff --check`: **passed**.
- The runtime matrix contains no token material and remains descriptive; all
  physical writes continue through the existing semantic admission boundary.

## Residual risk

Remote exposure still requires deployment-owned HTTPS, token records and
network controls. `domotics://runtime` is an inventory/diagnostic contract,
not actuator authority; provider visibility must never be interpreted as
permission to bypass plan validation, approval, freshness or safety gates.
