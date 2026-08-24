# System Composition Quality

This repository is composition-aware.

A significant change must be validated across subsystem boundaries, not only
inside the feature that was edited.

## Baseline tooling

Depending on the detected ecosystem:

- Python: Import Linter + pytest + Testcontainers + Pact Python.
- JavaScript/TypeScript: dependency-cruiser + Vitest + Testcontainers + Pact JS.
- .NET: ArchUnitNET + existing test framework + Testcontainers + PactNet.
- Java: ArchUnit + JUnit 5 + Testcontainers + Pact JVM.
- Other ecosystems: the same composition principles are applied using
  project-appropriate equivalents.

## Required boundary review

Review:
- shared state and ownership
- transactions / compensation
- DB invariants and migrations
- event/message ordering
- retries / replay / duplicate delivery / idempotency
- timeouts / cancellation / error propagation
- cache invalidation
- concurrency / locking
- resource lifecycle
- API/schema/data contracts
- configuration propagation
- backward compatibility
- partial failure after earlier components already changed state

Generic no-cycle rules are only a baseline. Use the agent-assisted configure
command to derive actual domain/layer boundaries from code and architecture.

## Project-specific decisions

- **Pact: not adopted.** domoai is a single deployable process (one MCP
  server + in-process adapters); every external boundary (Home Assistant,
  KNX, Matter, Modbus, Zigbee2MQTT) is a third-party vendor system we do not
  control the provider side of, so there is no independently-versioned
  consumer/provider pair under our control to pin a Pact contract between.
  The MCP protocol surface itself is already covered by in-process contract
  tests (`tests/contract/test_*_mcp_contract.py`,
  `tests/contract/test_mcp_protocol_certification.py`). `pact-python` was
  removed from `dev` deps for this reason; add it back only if a second,
  independently-deployed domoai service appears.
- **Testcontainers: adopted for the MQTT boundary.** See
  `tests/composition/test_zigbee2mqtt_broker_composition.py`, which runs the
  real `Zigbee2MqttAdapter` + `AiomqttTransport` against a disposable
  `eclipse-mosquitto` container instead of `InMemoryMqttTransport`, to catch
  wire-level bugs (topic wildcards, retained-flag handling) the in-memory
  mock cannot. Marked `@pytest.mark.composition`; skips when Docker is
  unreachable. Modbus/KNX/Matter/Home Assistant already have opt-in
  env-var-gated live smoke tests under `tests/integration/`; converting those
  to Testcontainers is future work (no ready-made simulator image was
  verified for this pass).
- **Import Linter: layers contract reflects the real architecture, not an
  aspirational one.** `domoai.runtime` is currently both the low-level
  kernel (clock, ports, state_store, registry — depended on by
  adapters/optimizer/persistence/config) and an orchestration layer
  (executor, event_consumer, twin, replay, scheduler, bundle_commit,
  policy_engine) that imports `application`/`adapters`/`persistence`/
  `config`. The `layers` contract in `.importlinter` is intentionally left
  strict, so this shows up as BROKEN under `uv run lint-imports` rather than
  being defined away. Migration path: split `domoai.runtime` into a pure
  kernel package and move the orchestration modules into `domoai.application`
  (they already depend on it).
  **CI status (spec 149):** the CI job that used to be named "Architecture
  contracts" only ever ran `scripts/check_architecture_contracts.py` (domain
  purity + adapter independence + `.importlinter` presence) — it never ran
  `lint-imports`, so the layers/acyclic-siblings contracts were not actually
  gating merges despite the name implying they were. It is now named "Source
  architecture invariants" to describe what it actually checks, and a
  separate non-blocking `import-linter-diagnostic` job runs `lint-imports` on
  every PR so the known violation stays visible in CI logs. Branch
  protection's required checks were updated to the new name.

## Commands

- project-composition <project>
- project-composition-doctor <project>
- project-composition-configure <project> claude|codex
- project-composition-check <project>
- project-composition-review <project> claude|codex
- project-composition-full <project> claude|codex
