# Declarative Connector Recipes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a new connector mostly configuration when its transport and device mapping are known, while preserving a custom-code escape hatch for proprietary products.

**Architecture:** Add a strict, secret-free recipe model describing transport, discovery, authentication and capability mappings. A recipe provider implements the existing five provider methods by delegating to bounded discovery/transport engines; the resulting adapter still implements `AdapterPort`, so the agent only sees DomoAI's normalized semantic tools. Start with MQTT as the pilot and add REST/OpenAPI after the recipe contract is proven.

**Tech Stack:** Python 3.12, Pydantic v2, Starlette, httpx, existing MQTT adapter/mapping code, Python entry points, pytest/pytest-asyncio, ruff, mypy and Import Linter.

**Spec:** [connector detection audit](../../auditoria-conectores-deteccion-generica-2026-09-09.md), [approved onboarding spec](../../../specs/205-connector-onboarding/spec.md), [provider SDK](../../connection-provider-sdk.md)

## Global Constraints

- Preserve `ConnectionProviderPort` v1 and its five async methods.
- `ConnectionManager` remains the only path for persistence, credential storage, activation and runtime mutation.
- The agent-facing boundary remains `AdapterPort` plus normalized MCP tools; recipes must not register arbitrary vendor tools.
- Recipe files and manifests contain no tokens, passwords, client secrets or complete credential-bearing URLs.
- No `eval`, executable templates, arbitrary Python snippets or unrestricted network writes are allowed in a recipe.
- Every write mapping must declare its allowed operation, input validation and readback strategy.
- Unknown devices, incomplete mappings and ambiguous capabilities fail closed and produce sanitized diagnostics.
- Discovery is bounded by interface selection, timeout, response-size, rate and cancellation limits.
- Existing static providers, P0/P1/P2 connection flows and zero-provider boot behavior remain compatible.
- The first implementation targets MQTT; REST/OpenAPI is a separate phase. Modbus, KNX, Matter, BLE, Thread and proprietary protocols use the same contract but are not silently claimed as recipe-ready until their engines exist.
- Do not commit, push or publish changes unless the user explicitly requests it.

## File and Boundary Map

```text
src/domoai/domain/connector_recipes.py          recipe and mapping models
src/domoai/runtime/connector_recipe_ports.py    discovery/transport engine ports
src/domoai/application/connector_recipes.py     recipe loading and provider composition
src/domoai/adapters/mqtt/recipe_adapter.py      MQTT recipe adapter bridge
src/domoai/adapters/rest/recipe_adapter.py      later REST/OpenAPI adapter
src/domoai/admin/connector_scaffold.py           recipe-aware authoring/checking
src/domoai/management/api.py                     catalog and recipe diagnostics
schemas/v1/                                      generated public schemas
tests/unit/domain/test_connector_recipes.py
tests/unit/runtime/test_connector_recipe_engines.py
tests/contract/test_connector_recipe_contract.py
tests/security/test_connector_recipe_safety.py
tests/integration/test_mqtt_recipe_connector.py
tests/integration/test_rest_recipe_connector.py
tests/composition/test_connector_recipe_composition.py
```

The existing `ConnectionProviderBase`, `ConnectionProviderLoader`,
`ConnectionManager`, `ManagedAdapterSet`, `CompositeAdapter` and
`domotics_server.py` remain owners of their current boundaries. New recipe
modules call them; they do not replace them.

## Phase 1: Recipe Contract and Safety Foundation

### Task 1: Define the strict recipe models

**Files:**
- Create: `src/domoai/domain/connector_recipes.py`
- Test: `tests/unit/domain/test_connector_recipes.py`
- Test: `tests/contract/test_connector_recipe_contract.py`

**Interfaces:**
- Produces `TransportFamily`, `DiscoveryStrategy`, `AuthScheme`,
  `CapabilityBinding` and `CommandBinding` with explicit readback fields, and
  `ConnectorRecipe` Pydantic models.
- `ConnectorRecipe` contains only public metadata: connector ID, transport
  family, discovery strategies, authentication schemes and mappings.

- [x] **Step 1: Write the failing model tests.**

  Test that a valid MQTT recipe parses, duplicate capability IDs are rejected,
  secret values and credential-bearing URLs are rejected, unsupported HTTP
  methods are rejected, and a write mapping without readback is rejected.

- [x] **Step 2: Run the focused tests and verify they fail.**

  Run: `uv run pytest -q tests/unit/domain/test_connector_recipes.py tests/contract/test_connector_recipe_contract.py`
  Expected: collection or assertion failures because the recipe models do not
  exist yet.

- [x] **Step 3: Implement the smallest strict models.**

  Use enums for `mqtt`, `rest`, `websocket`, `modbus`, `knx`, `matter`,
  `serial` and `custom`; use explicit strategy values for `manual`, `mdns`,
  `ssdp`, `dhcp`, `mqtt_discovery` and `matter_commissioning`. Use bounded
  string templates for paths/topics and reject braces that do not name an
  approved variable such as `{device_id}`.

- [x] **Step 4: Run the focused tests and verify they pass.**

  Run: `uv run pytest -q tests/unit/domain/test_connector_recipes.py tests/contract/test_connector_recipe_contract.py`
  Expected: PASS.

### Task 2: Add engine ports and recipe validation

**Files:**
- Create: `src/domoai/runtime/connector_recipe_ports.py`
- Create: `src/domoai/application/connector_recipes.py`
- Modify: `src/domoai/runtime/connection_ports.py`
- Test: `tests/unit/runtime/test_connector_recipe_engines.py`
- Test: `tests/security/test_connector_recipe_safety.py`

**Interfaces:**
- Produces `DiscoveryStrategyPort.discover(recipe, setup)` returning
  secret-safe candidates.
- Produces `TransportRecipePort.build_adapter(recipe, setup, adapter_id)`
  returning `AdapterPort`.
- Produces `validate_connector_recipe(recipe)` returning stable diagnostics.

- [x] **Step 1: Write failing tests for engine isolation and recipe checks.**

  Cover unsupported strategy, missing engine, unknown mapping variable,
  credential in a topic/path, network destination outside the configured
  allowlist, and a broken recipe that must not prevent another provider from
  loading.

- [x] **Step 2: Run the tests to verify the failures.**

  Run: `uv run pytest -q tests/unit/runtime/test_connector_recipe_engines.py tests/security/test_connector_recipe_safety.py`
  Expected: FAIL because the ports and validator are absent.

- [x] **Step 3: Implement ports and bounded validation.**

  Keep discovery and transport registries keyed by enum value. Return
  sanitized `ConnectionDiagnostic` objects. Do not make a network call during
  recipe validation. Add an optional recipe reference to
  `ConnectionProviderManifest` without changing the v1 contract version.

- [x] **Step 4: Run the tests to verify the pass.**

  Run: `uv run pytest -q tests/unit/runtime/test_connector_recipe_engines.py tests/security/test_connector_recipe_safety.py`
  Expected: PASS.

### Task 3: Export schemas and expose recipe metadata

**Files:**
- Modify: `scripts/export_schemas.py`
- Modify: `src/domoai/management/api.py`
- Modify: `src/domoai/management/ui.py`
- Test: `tests/contract/test_schema_exports.py`
- Test: `tests/integration/test_management_api.py`
- Test: `tests/integration/test_management_ui.py`

- [x] **Step 1: Add failing assertions for recipe metadata.**

  Assert that the provider catalog includes transport family, discovery
  strategies and recipe diagnostics without including credential fields or
  submitted values.

- [x] **Step 2: Implement additive catalog projection and schema exports.**

  Keep the current catalog response compatible. Render recipe setup fields
  through the existing descriptor mechanism; do not create a provider-specific
  page.

- [x] **Step 3: Run the focused API, UI and schema tests.**

  Run: `uv run pytest -q tests/contract/test_schema_exports.py tests/integration/test_management_api.py tests/integration/test_management_ui.py`
  Expected: PASS.

## Phase 2: MQTT Declarative Pilot

### Task 4: Implement MQTT discovery strategy

**Files:**
- Create: `src/domoai/adapters/mqtt/recipe_discovery.py`
- Modify: `src/domoai/application/connector_recipes.py`
- Test: `tests/unit/adapters/mqtt/test_recipe_discovery.py`
- Test: `tests/security/test_connector_recipe_safety.py`

**Interfaces:**
- `MqttRecipeDiscovery.discover(recipe, setup) -> list[ConnectionCandidate]`.
- The strategy accepts a broker setup and a declared discovery prefix/topic;
  it never scans arbitrary brokers or subscribes to unbounded wildcards.

- [x] **Step 1: Write failing tests using an injected MQTT transport.**

  Cover valid discovery payloads, duplicate IDs, malformed JSON, oversized
  payloads, timeout/cancellation, and a payload containing a token that must
  not appear in the candidate or diagnostic.

- [x] **Step 2: Implement bounded discovery and normalization.**

  Reuse the existing MQTT codec/configuration types. Require a stable device
  identifier and preserve the source topic as non-secret metadata only.

- [x] **Step 3: Run the tests.**

  Run: `uv run pytest -q tests/unit/adapters/mqtt/test_recipe_discovery.py tests/security/test_connector_recipe_safety.py`
  Expected: PASS.

### Task 5: Bridge MQTT recipes to the existing adapter

**Files:**
- Create: `src/domoai/adapters/mqtt/recipe_adapter.py`
- Modify: `src/domoai/adapters/mqtt/adapter.py`
- Modify: `src/domoai/adapters/mqtt/config.py`
- Test: `tests/integration/test_mqtt_recipe_connector.py`
- Test: `tests/contract/test_provider_sdk.py`

**Interfaces:**
- `MqttRecipeAdapter(AdapterPort)` maps recipe bindings to the existing
  normalized `AdapterSnapshot`, `StateSnapshot`, `Command` and event models.
- It supports only declared read topics and command topics; it never executes
  a topic or payload assembled from arbitrary user text.

- [x] **Step 1: Write the failing end-to-end fixture test.**

  Start from a recipe and manual broker candidate, build the adapter, discover
  one light and one sensor, read their states, execute one allowed light
  command, receive the event and prove readback. Also test an unmapped device
  and a rejected command.

- [x] **Step 2: Implement the bridge by reusing existing MQTT mappings.**

  Convert recipe bindings into `MqttAdapterMapping` at validation time. Keep
  credential resolution inside `ConnectionManager`/the configured secret
  store. Use stable adapter IDs and preserve source provenance.

- [x] **Step 3: Run the integration and contract tests.**

  Run: `uv run pytest -q tests/integration/test_mqtt_recipe_connector.py tests/contract/test_provider_sdk.py`
  Expected: PASS.

### Task 6: Generate and check a recipe-based connector

**Files:**
- Modify: `src/domoai/admin/connector_scaffold.py`
- Modify: `src/domoai/admin/cli.py`
- Modify: `docs/connection-provider-sdk.md`
- Test: `tests/contract/test_connector_authoring_contract.py`
- Test: `tests/integration/test_connector_scaffold.py`

- [x] **Step 1: Write failing CLI tests.**

  Cover `connector create --recipe recipe.yaml`, generated recipe metadata,
  a valid MQTT check, an incomplete mapping check, and rejection of executable
  recipe content.

- [x] **Step 2: Add recipe-aware generation and conformance.**

  Generate a provider that delegates the five methods to recipe engines and
  reports `incomplete` until every declared mapping has an adapter path and
  tests. Preserve the existing code-based scaffold command unchanged.

- [x] **Step 3: Update SDK examples and run tests.**

  Run: `uv run pytest -q tests/contract/test_connector_authoring_contract.py tests/integration/test_connector_scaffold.py`
  Expected: PASS.

## Phase 3: REST/OpenAPI Recipe

### Task 7: Add a bounded REST/OpenAPI recipe adapter

**Files:**
- Create: `src/domoai/adapters/rest/__init__.py`
- Create: `src/domoai/adapters/rest/recipe_adapter.py`
- Create: `src/domoai/adapters/rest/openapi_profile.py`
- Test: `tests/unit/adapters/rest/test_openapi_profile.py`
- Test: `tests/integration/test_rest_recipe_connector.py`
- Test: `tests/security/test_rest_recipe_safety.py`

**Interfaces:**
- `OpenApiRecipeProfile` parses a supplied OpenAPI document into an allowlisted
  set of read, command and readback operations.
- `RestRecipeAdapter(AdapterPort)` uses only those operations and validates
  response schemas before normalizing state.

- [x] **Step 1: Write failing profile and safety tests.**

  Cover valid OpenAPI 3.1 paths, missing response schemas, path traversal,
  unexpected HTTP methods, redirects, oversized documents, credential-bearing
  servers, non-allowlisted hosts and a write without readback.

- [x] **Step 2: Implement profile parsing without code generation or eval.**

  Accept only JSON/YAML structures supported by the declared recipe subset.
  Resolve servers against the operator-provided endpoint, enforce HTTPS for
  non-local deployments, cap document and response sizes, and use httpx with
  bounded timeouts and no unsafe redirects.

- [x] **Step 3: Implement normalized reads, commands and readback.**

  Require explicit JSON paths and type/unit conversion. Reject unknown
  capabilities and responses that do not satisfy the declared schema. Route
  all activation through `ConnectionManager`.

- [x] **Step 4: Run REST unit, integration and security tests.**

  Run: `uv run pytest -q tests/unit/adapters/rest tests/integration/test_rest_recipe_connector.py tests/security/test_rest_recipe_safety.py`
  Expected: PASS.

## Phase 4: Completion and Composition Gates

### Task 8: Verify runtime composition and agent exposure

**Files:**
- Test: `tests/composition/test_connector_recipe_composition.py`
- Test: `tests/integration/test_provider_catalog_onboarding.py`
- Modify: `docs/superpowers/plans/2026-09-09-declarative-connector-recipes.md`

- [x] **Step 1: Write cross-boundary scenarios.**

  Prove recipe provider loading beside a broken provider, catalog rendering,
  credential storage, activation into `ManagedAdapterSet`, inventory/state
  publication, semantic command routing, readback, reconnect and clean detach.

- [x] **Step 2: Run the focused composition tests.**

  Run: `uv run pytest -q tests/composition/test_connector_recipe_composition.py tests/integration/test_provider_catalog_onboarding.py`
  Expected: PASS.

- [x] **Step 3: Run repository quality gates.**

  Run: `uv run ruff check .`, `uv run mypy src`, `uv run lint-imports` and
  `scripts/composition_check.sh --fast`.
  Expected: all commands pass. Docker-backed physical qualification remains a
  separate external gate.

  Observed: all listed gates pass. The required full composition gate passes
  with `587 passed, 18 skipped` when invoked through the repository's
  `scripts/with_disposable_docker_config.sh`, which removes the dependency on
  a host-installed `docker-credential-desktop.exe` helper.

### Task 9: Update operator and author documentation

**Files:**
- Modify: `docs/connection-provider-sdk.md`
- Modify: `README.md`
- Modify: `docs/DomoAI-Connection-Control-Plane-v1.md`
- Create: `docs/connector-recipes.md`
- Modify: `specs/205-connector-onboarding/quickstart.md`

- [x] **Step 1: Document the three connector tiers.**

  Explain declarative recipe, known transport plus product profile, and custom
  proprietary adapter. Include one MQTT example and one REST/OpenAPI example.

- [x] **Step 2: Document the hard boundary.**

  State that discovery finds candidates, adapters find devices, and the agent
  receives normalized capabilities. State explicitly that unknown semantics
  never become executable commands automatically.

- [x] **Step 3: Validate links, examples and generated schemas.**

  Run: `uv run python scripts/export_schemas.py` and the documentation contract
  tests. Expected: all referenced commands and files exist, and no example
  contains a real secret.

## Self-Review Checklist

- [x] The plan covers the audit findings CON-001 through CON-007.
- [x] MQTT is independently usable before REST/OpenAPI work begins.
- [x] Every recipe write has explicit validation and readback.
- [x] No task bypasses `ConnectionManager`, `AdapterPort` or MCP policy.
- [x] Broken recipe/provider isolation is tested.
- [x] Physical KNX/HIL qualification is recorded as external, not implied by
  software tests.
- [x] Search for unfinished-marker patterns and placeholder function names
 returns no unresolved planning markers before execution starts.
