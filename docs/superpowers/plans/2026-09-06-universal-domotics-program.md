# Universal Domotics MCP Program Implementation Plan

> **For agentic workers:** Each phase is an independently approved Spec Kit
> increment. Do not implement more than one phase per change set.

**Goal:** Close the confirmed product gaps while preserving one general MCP
endpoint and one physical-execution authority for every AI and domotics stack.

**Architecture:** The public MCP and the agent-facing adapter stay unified.
Generic MQTT and every protocol/vendor integration enter as internal connectors
behind the same `AdapterPort` and canonical semantic model; no public adapter or
MCP is created per vendor. Prompts and host-specific Skill wrappers guide
agents but never create commands or authority; an external event bus remains a
gated decision after measured evidence.

**Tech Stack:** Python 3.12, FastMCP, Pydantic v1 schemas, asyncio, aiomqtt,
SQLite/PostgreSQL composition ports, OR-Tools, pytest, Testcontainers.

**Spec:** [universal-domotics-coverage design](../specs/2026-09-06-universal-domotics-coverage-design.md)

## Global Constraints

- Public transport remains one standard MCP endpoint; no Claude/Codex-specific
  API is introduced.
- All writes go through the existing plan, policy, approval, admission,
  executor, readback and audit chain.
- There is one universal semantic adapter; internal connectors never generate
  vendor-specific MCP tools or authority paths.
- Pydantic domain contracts are authoritative; generate reviewed schemas after
  every public-contract change.
- Keep local deterministic fixtures working without a broker, cloud service or
  hardware.
- Test-first: add contract, integration and composition coverage before each
  behavior change.
- Do not enable active-active or declare HIL/product hardware readiness from
  local tests.

---

## Program order

```text
P0 baseline ─┬─ P1 MCP prompts/resources ─ P4 host portability
             ├─ P2 generic MQTT ───────────────┬─ P6 qualification
             └─ internal connector hardening ───┘
             └─ P5 event-fabric measurement gate
```

P0 is the only mandatory prerequisite. P2 is the first functional expansion.
P5 produces a decision, not an automatic infrastructure migration.

### Phase 0: Baseline MCP contract and coverage inventory

**Spec to create:** `specs/196-universal-mcp-baseline/`.

**Files expected to change:**

- `docs/PENDIENTES.md`
- `docs/unified-mcp.md`
- `docs/contracts.md`
- `docs/adapter-sdk.md`
- `tests/contract/test_unified_mcp_contract.py`
- `tests/contract/test_gateway_deployment_assets.py`
- new `docs/adapter-coverage.md`

**Tasks:**

1. Replace the obsolete v1 status in `docs/PENDIENTES.md` with the current
   audit/comparative links and a status matrix: native, via Home Assistant,
   plugin, fixture, qualified hardware.
2. Add a contract test asserting the configured gateway exposes one MCP path
   and that the domotics and optimizer contexts share the same registry and
   `PlanService` instance.
3. Document the safe semantic lifecycle: resource/discovery read,
   preview, prepare, approval, execute/schedule. State explicitly why there
   is no direct `set_state` or `execute_command` physical bypass.
4. Run documentation-contract, gateway-contract and architecture checks.

**Acceptance:** documentation names the actual supported adapters and a
regression fails if a second public execution runtime is introduced.

### Phase 1: Standard MCP prompts and capability coverage resources

**Spec to create:** `specs/197-mcp-prompts-coverage/`.

**Files expected to change:**

- `src/domoai/mcp/unified_server.py`
- `src/domoai/mcp/domotics_server.py`
- `src/domoai/mcp/resources.py`
- `src/domoai/mcp/configured.py`
- `tests/contract/test_unified_mcp_contract.py`
- `tests/contract/test_domotics_mcp_contract.py`
- new `tests/contract/test_mcp_prompts_contract.py`
- `docs/unified-mcp.md`

**Tasks:**

1. Define typed, bounded coverage projection from registry/routes/runtime
   health. It must label a route as active, unavailable, simulated, indirect
   through HA or requiring external qualification without exposing secrets.
2. Write contract tests that list prompts/resources through a generic MCP
   client and assert prompt invocation returns guidance only, never a plan ID,
   approval, adapter call or state mutation.
3. Register versioned prompts for inventory discovery, safe device diagnosis
   and energy-plan preparation. Each prompt references semantic tool names,
   not vendors or agent-host APIs.
4. Publish `domotics://coverage` and document client-neutral discovery.
5. Run MCP contract, integration parity and gateway HTTP tests.

**Acceptance:** any MCP client can enumerate a stable semantic catalog and
receive safe procedures without gaining authority.

### Phase 2: Generic declarative MQTT adapter

**Spec to create:** `specs/198-generic-mqtt-adapter/`.

**Files expected to change:**

- new `src/domoai/adapters/mqtt/config.py`
- new `src/domoai/adapters/mqtt/codec.py`
- new `src/domoai/adapters/mqtt/mapper.py`
- new `src/domoai/adapters/mqtt/adapter.py`
- new `src/domoai/adapters/mqtt/__init__.py`
- `src/domoai/adapters/zigbee2mqtt/transport.py` (only shared transport
  extraction if tests prove it is necessary)
- `src/domoai/application/runtime_factory.py`
- `src/domoai/config/settings.py`
- `schemas/v1/` and `scripts/export_schemas.py`
- new unit, contract, integration and composition tests under
  `tests/{unit/adapters,contract,integration,composition}/`
- `dev/lab/` and deployment examples

**Interfaces:** a mapping declares source identity, read/write topics, state
codec, availability, capabilities and explicit readback expectation; the
adapter implements the existing `AdapterPort` exactly.

**Tasks:**

1. Write failing configuration tests: reject unknown fields, duplicate source
   identity, wildcard write topics, unbounded payloads, missing units/ranges,
   commands without idempotency and readback-required writes without feedback.
2. Define a strict v1 mapping model and export its schema.
3. Implement pure codecs/mappers that convert mapping plus MQTT frames into
   `AdapterSnapshot`, `StateSnapshot` and typed `SourceEvent`; rejected frames
   become sanitized diagnostics, never state.
4. Implement adapter lifecycle, bounded subscribe queues, discovery, current
   state reads, write acknowledgement and correlated readback using the
   existing MQTT transport abstraction without importing Zigbee2MQTT mapping
   code.
5. Wire settings/factory only when a complete MQTT mapping is configured;
   incomplete configuration must fail before connection.
6. Add deterministic ESP-like fixture plus Mosquitto/Testcontainers scenarios:
   normal read/write/readback, stale/duplicate/out-of-order frame, broker
   reconnect, malformed payload, duplicate idempotency key and conflict with
   an HA source identity.
7. Update lab, deployment, adapter-SDK and coverage documentation.

**Acceptance:** an MQTT light, sensor, cover and safe actuator are discoverable
and controllable through existing semantic plans only; no MQTT-specific MCP
tool exists.

### Phase 3: Internal connector hardening (no public adapter expansion)

Esta fase reemplaza el antiguo plan de crear plugins o adapters directos por
fabricante. No se abrirá un catálogo de adapters públicos. Sólo se aceptarán
mejoras internas del `Universal Adapter` y su SDK cuando amplíen la cobertura
semántica sin cambiar la superficie MCP ni crear una autoridad paralela.

Las pruebas de conformance, identidad, readback, idempotencia y redacción de
errores siguen siendo válidas para esos conectores internos. La incorporación
de un fabricante concreto no es un entregable ni una deuda del objetivo.

### Phase 4: Portable Skills and host integration kits

**Spec to create:** `specs/200-portable-host-skills/`.

**Files expected to change:**

- `skills/core/README.md`
- `src/domoai/skills/catalog.py`
- `src/domoai/skills/validator.py`
- new `skills/claude/README.md`
- new `skills/codex/README.md`
- new `clients/claude-code/README.md`
- new `clients/codex/README.md`
- new `clients/generic-mcp/README.md`
- `tests/contract/test_skill_catalog.py`
- `tests/contract/test_skill_contract.py`
- `README.md`, `docs/unified-mcp.md`

**Tasks:**

1. Define the portable-core contract and extension metadata: host, minimum
   capability, declared deviations and a ban on credentials/direct adapter
   references.
2. Add fixture host wrappers that reference, but do not fork, core workflows.
3. Add configuration examples for stdio and authenticated gateway HTTP using
   placeholders only; document scopes, TLS and one-time token rotation.
4. Test that the same core workflow emits the same ordered semantic MCP
   operations through each wrapper and preserves blocked approval behavior.
5. Test rejection of a wrapper that changes an operation, reorders approval,
   exposes a secret-shaped field or names a vendor adapter.

**Acceptance:** changing host changes onboarding/presentation only, never the
semantic execution contract.

### Phase 5: Event-fabric measurement and decision gate

**Spec to create:** `specs/201-event-fabric-decision/`.

**Files expected to change:**

- `src/domoai/application/event_consumer.py`
- `src/domoai/runtime/composite_adapter.py`
- `src/domoai/runtime/operational_metrics.py`
- `src/domoai/mcp/resources.py`
- `src/domoai/mcp/remote_metrics.py`
- new performance/composition scenarios and report documentation

**Tasks:**

1. Define bounded measurements: event rate, queue depth, coalescing/drop,
   source lag, reconnect/recovery latency, persistence latency and per-home
   isolation. Do not add unbounded labels.
2. Add deterministic load/fault scenarios for all current queues and verify
   stale/unknown behavior when a source falls behind or reconnects.
3. Set explicit thresholds and generate a decision report with `retain_local`,
   `investigate` or `specify_external_bus` outcome.
4. Verify the external-bus choice is absent from normal settings and no metric
   report can enable it.

**Acceptance:** a reproducible report proves whether the local event fabric
meets declared operating targets. A bus implementation is forbidden unless the
outcome is `specify_external_bus` and receives a separate approved Spec.

### Phase 6: External protocol and hardware qualification

**Specs to execute/extend:** `133-battery-hil-certification`,
`140-real-composition-tests`, `141-provider-contract-tests`, plus one
qualification spec per new plugin/direct adapter.

**Files expected to change:** HIL profiles/evidence, qualification service,
deployment runbooks, external test contracts and evidence reports; no
production behavior is relaxed merely to make a test pass.

**Tasks:**

1. Run attended HIL against declared battery/inverter hardware and archive
   identity, profile/firmware, command, readback, timing, takeover and restart
   evidence.
2. Run each critical adapter against its real disposable/deployed dependency;
   preserve skips as `blocked_external_dependency`.
3. Verify independently deployed provider contracts and compatibility before
   marking an adapter profile production-qualified.
4. Re-run preflight and confirm unavailable hardware remains blocked.

**Acceptance:** qualification is specific to hardware/profile/firmware and
expires on evidence mismatch; fixtures, simulations and skipped tests cannot
grant physical authority.

## Cross-phase verification

After each phase: focused unit/contract/integration/composition tests, schema
export/check, `uv run ruff check .`, `uv run mypy src`, architecture contracts,
runtime-document checks and final diff review. For any phase that changes
shared state, events, adapters, MCP contracts or deployment, also run:

```bash
project-composition-check "$(cat .ai/project-name)"
```

Run `system-composition-review` before declaring that phase complete. Full
hardware qualification remains a separately evidenced external gate.
