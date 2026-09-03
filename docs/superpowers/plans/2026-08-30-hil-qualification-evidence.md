# HIL Qualification Evidence Implementation Plan

> Execute with tests first and keep the lab fixtures unchanged.

**Goal:** Ensure HIL evidence describes exactly the binding/runtime/provider
that was exercised and cannot turn operator labels or incomplete manual notes
into production qualification.

**Spec:** `specs/158-physical-authority-program/blocks/06-hil-qualification.md`

## Tasks

- [x] Add adversarial tests for provider-mismatched takeover baselines,
  structured manual statuses and provider-observed identity.
- [x] Pass the CLI-selected binding explicitly into runtime construction and
  enforce profile/deployment power ceilings.
- [x] Validate takeover provider, device, capability, baseline observation and
  live lease before marking the HIL check successful.
- [x] Guarantee final stop/readback cleanup on unexpected sequence failures.
- [x] Require identity observation digest, binding/profile, provider, software
  version and freshness for qualification.
- [x] Regenerate schemas and run HIL, qualification, contract and composition
  tests.

## Verification record

- Focused HIL/qualification/schema suite: **24 passed**.
- B05 guard/terminal/worker suite: **16 passed**.
- `uv run mypy src`: **Success: no issues found in 132 source files**.
- Targeted Ruff and contract tests: **passed**.
- Remaining full-suite and composition-gate verification is recorded in the
  parent runtime plan after all current block changes are included.

## Residual risk

The identity digest is an integrity binding, not a hardware-backed signature.
Production elevation still requires a trusted test authority or attestation
outside the local CLI.
