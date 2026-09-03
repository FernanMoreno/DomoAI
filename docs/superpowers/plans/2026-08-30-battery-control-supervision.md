# Battery Control Supervision Implementation Plan

> **For agentic workers:** execute the checked tasks with tests first and run
> the composition gates before marking B04 complete.

**Goal:** Treat battery and EV-like latched actuators as physically owned
resources: startup reconciliation, provider-specific takeover, bounded lease
supervision, emergency stop and verified zero-power readback.

**Architecture:** Keep `BatteryControlCoordinator` as the single runtime
authority for battery takeover. A configured binding selects one concrete
provider adapter; a composite is only a routing container. Before any new
lease, the coordinator requires a successful startup reconciliation when
physical feedback is configured. Emergency stop is successful only after a
fresh provider readback confirms the safe value.

**Spec:** `specs/158-physical-authority-program/blocks/04-battery-control-supervision.md`

## Constraints

- `dev/lab/` and the live KNX/Home Assistant setup remain unchanged.
- A mapping route never grants actuator authority by itself.
- ACK from the transport is not physical stop evidence.
- Failed startup reconciliation permanently blocks new acquisition for that
  runtime instance.
- Existing user changes outside this block must be preserved.

## Tasks

- [x] Add adversarial tests for startup reconciliation gating, mandatory
  readback and provider routing through a composite.
- [x] Make startup reconciliation a durable-in-process admission gate; a
  failed or unperformed reconciliation cannot acquire a battery lease.
- [x] Make emergency stop fail closed when physical feedback/readback is not
  available, and ensure failed confirmation keeps authority revoked.
- [x] Make provider selection fail closed instead of falling back to the
  composite adapter.
- [x] Verify simulator/composition/unit suites plus architecture and contract
  gates.
- [x] Update B04 status, contracts and the plan with final evidence only
  after the cross-subsystem composition review.

## Verification record

- `uv run pytest -q` (composition gate): **958 passed**.
- B04 focused unit/contract/integration/composition selection: **119 passed**.
- `uv run mypy src`: **Success: no issues found in 132 source files**.
- `uv run lint-imports`: **4 kept, 0 broken**.
- `uv run python scripts/check_architecture_contracts.py`: **passed**.
- `uv run python scripts/check_runtime_contract_docs.py`: **passed**.
- `git diff --check`: **passed**.
- `uv run ruff check .`: **passed after import normalization**.
- The repository has no installed `project-composition-check` or
  `project-composition-review` executable; `scripts/composition_check.sh
  --fast` was used as the repository fallback.
