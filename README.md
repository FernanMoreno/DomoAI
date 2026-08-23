# DomoAI

Universal agentic domotics runtime with a semantic device model, multi-adapter composition and one general MCP interface.

## Development environment

This project uses [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy src
```

The runtime dependencies include the MCP Python SDK, Pydantic, Home Assistant HTTP/WebSocket clients, `aiomqtt` for the optional Zigbee2MQTT adapter, JSON Schema validation and OR-Tools. Local SQLite persistence uses Python's standard library. Development tools are installed through uv's default `dev` dependency group.

To add or update a dependency, edit `pyproject.toml` and regenerate the lockfile:

```bash
uv lock
uv sync
```

## Local MCP server

The semantic MCP server can be launched over `stdio`. Without Home Assistant
settings it uses the deterministic fixture:

```bash
uv run domoai-mcp
```

Example host configuration:

```json
{
  "mcpServers": {
    "domoai": {
      "command": "uv",
      "args": ["run", "domoai-mcp"],
      "cwd": "/path/to/DomoAI"
    }
  }
}
```

The same command can be registered in Claude Code, Codex or another compatible
MCP client.

## Unified MCP surface

The single `domoai-mcp` server exposes discovery, state, energy context,
policy-aware plan validation/execution and the proposal-only OR-Tools tools
`validate_scenario`, `optimize_scenario` and `explain_solution` through the same
MCP session. Register exactly one server in Claude Code, Codex or any other
compatible MCP client that supports local stdio:

```json
{
  "mcpServers": {
    "domoai": {
      "command": "uv",
      "args": ["run", "domoai-mcp"],
      "cwd": "/path/to/DomoAI"
    }
  }
}
```

OR-Tools remains an internal proposal/validation/explanation layer. It cannot
execute a device, approve a plan or call an adapter, and there is no second
public OR-Tools MCP endpoint.

The portable `optimize-home-energy` skill routes every DomoAI operation through
one `mcp` role. Its reference workflow is validated locally with deterministic
in-process fixtures:

```bash
uv run pytest -q tests/contract/test_skill_contract.py
uv run pytest -q tests/integration/test_energy_skill_workflow.py
```

The workflow uses the same connection for semantic reads, proposals,
explanations and plan validation. The published v3 skill hands every mutation
to `commit_or_schedule_bundle`; it never calls an adapter directly. Sensitive
bundles pause for explicit operator approval.

For energy-aware scenarios, the portable v3 procedure reads a complete typed
context through `mcp.get_energy_context` before calling the proposal-only
optimizer. The context aligns tariffs and solar forecasts to a fixed horizon
and may include one battery profile. CP-SAT returns cost, peak-import and
solar-self-consumption evidence plus per-slot energy balance; it never calls a
physical adapter. Context failure, revision mismatch, infeasibility or solver
timeout stops before validation and execution. The deterministic provider and
focused acceptance commands are covered by the repository contract and
integration tests.

### One-time solar profile for live energy data

OMIE tariffs and Open-Meteo forecasts are collected automatically whenever the
energy context is requested. Only the physical installation metadata needs to
be supplied once. Copy the example, replace its placeholder values with the
inverter or installer data, and point the runtime at it:

```bash
cp config/solar-profile.example.json config/solar-profile.json
export DOMOAI_ENERGY_LIVE=1
export DOMOAI_TARIFF_PROVIDER=omie
export DOMOAI_SOLAR_PROVIDER=open_meteo
export DOMOAI_SOLAR_PROFILE_PATH=config/solar-profile.json
uv run domoai-mcp
```

The profile is strict, versioned and credential-free. It must contain real
installation values before using the result for optimization; the example's
Madrid values only document the shape. The older individual `DOMOAI_SOLAR_*`
variables remain available as a mutually exclusive compatibility fallback.

Physical battery dispatch is `software-qualified` when its server-owned
binding is configured. It becomes `hil-qualified` only with matching complete
inverter evidence. Set `DOMOAI_BATTERY_DISPATCH_PRODUCTION=1` together with
`DOMOAI_BATTERY_HIL_EVIDENCE_PATH` only after the opt-in HIL run has passed;
the runtime fails closed otherwise. A deterministic test run never certifies
real hardware.

Dispatchable battery control is opt-in through a complete, server-owned
canonical profile:

```bash
export DOMOAI_BATTERY_DISPATCH_PROFILE_PATH=config/dispatchable-battery-profile.json
uv run domoai-mcp
```

The profile is strict v1 JSON and must contain the canonical device, actuator,
feedback, SOC and capacity evidence. A mapping declaration alone never enables
physical dispatch; live energy mode, the profile and the runtime safety gates
are all required.

## Universal Provider SDK

Future Home Assistant, inverter and MQTT integrations must translate their
source-specific identities and payloads into the Provider SDK v1 boundary
before reaching the semantic runtime. The SDK reuses DomoAI's canonical
`DeviceType`, `Capability` and `SourceRef` models and separates providers into
telemetry and command roles:

```text
external provider
      ↓
ProviderManifest + DeviceDescriptor + Measurement
      ↓
ProviderRegistry (stable order, safe diagnostics)
      ↓
canonical runtime / StateStore / MCP / OR-Tools
```

Provider commands carry only bounded semantic parameters and an idempotency
key. They do not bypass `PlanService`, policy validation or `AdapterPort`.
The first concrete implementation is `HomeAssistantProvider`. It reuses the
authenticated REST/WebSocket client, groups entities by Home Assistant
`device_id` when registry metadata is available, and exposes only explicit
entity/capability metric mappings. It is the single Home Assistant integration
path: the provider is registered in `ProviderRegistry` and wrapped by
`HomeAssistantProviderAdapter`, so `DeviceRegistry`, `StateStore`, plan
execution and MCP keep one semantic path and one Home Assistant client.
See [`docs/adapter-sdk.md`](docs/adapter-sdk.md) and
[`docs/contracts.md`](docs/contracts.md) for the public boundary.

## Live Home Assistant runtime

Para desarrollo local sin hardware, el laboratorio virtual reproducible está
en [`dev/lab/README.md`](dev/lab/README.md) y su arranque mínimo cubre
Mosquitto/fake Zigbee2MQTT y PyModbus. Home Assistant, Matter Server y KNX
Virtual/ETS permanecen como perfiles manuales opt-in.

La ruta recomendada para operar ese laboratorio es el runner explícito:

```bash
uv run domoai-lab up
uv run domoai-lab status
uv run domoai-lab smoke
```

El smoke usa únicamente fixtures locales de Home Assistant, MQTT/Zigbee2MQTT,
Modbus, Matter y KNX; no inventa gateways, tokens ni commissioning. Los
smoke tests live siguen separados y requieren sus servicios y variables
`DOMOAI_*` reales.

The composition root selects the deterministic fixture when no live source is
configured, a direct adapter for one source, or a composite runtime for two or
more complete source configurations. Configure Home Assistant with:

```bash
export DOMOAI_HOME_ASSISTANT_URL="http://home-assistant.local:8123"
export DOMOAI_HOME_ASSISTANT_TOKEN="<long-lived-access-token>"
export DOMOAI_HOME_ASSISTANT_MAPPING_PATH="config/home-assistant-mappings.json"
export DOMOAI_DATABASE_PATH="data/domoai.sqlite3"
uv run domoai-mcp
```

The provider path is the configured Home Assistant runtime. The URL/token pair
is required and an optional strict v1 mapping document can make energy roles
explicit:

```json
{
  "schema_version": "v1",
  "metric_mappings": {
    "sensor.pv_power": {"power": "energy.pv.power"},
    "sensor.grid_power": {"power": "energy.grid.power"}
  }
}
```

The runtime authenticates REST service calls, persists plans, outcomes and
redacted audit events in SQLite, and runs the adapter event consumer in the
background. Supported write mappings currently include light/switch power and
toggle operations, light brightness, cover position/open/close/stop and climate
target temperature. An incomplete URL/token pair is rejected before startup.
Tokens are read as secret configuration and are never included in device,
command, outcome or audit payloads.

The Provider SDK path can be exercised independently of the runtime factory:

```python
provider = HomeAssistantProvider(
    HomeAssistantClient(base_url, token),
    metric_mappings={
        "sensor.pv_power": {"power": "energy.pv.power"},
        "sensor.battery_soc": {"battery": "battery.soc"},
    },
)
```

Only mapped sensor capabilities become canonical energy metrics. The client
also reads Home Assistant's enabled entity registry over WebSocket when state
payloads do not include `device_id`; registry identity is preserved when
provided, never inferred from names or areas.

The provider path is covered by deterministic fixtures. The opt-in live
provider-runtime smoke validates the same route against a real Home Assistant
instance without executing commands:

```bash
uv run pytest -q tests/integration/test_home_assistant_provider_smoke.py
```

It requires a real URL/token pair and keeps the token outside the repository.

## Live Zigbee2MQTT runtime

The native Zigbee2MQTT adapter is opt-in and supports the bounded v1 profile:
light/switch power, light brightness, temperature, humidity and occupancy.
Configure it alongside Home Assistant or another source:

```bash
export DOMOAI_ZIGBEE2MQTT_URL="mqtt://mqtt-broker.local:1883"
export DOMOAI_ZIGBEE2MQTT_BASE_TOPIC="zigbee2mqtt"
export DOMOAI_MQTT_TIMEOUT_SECONDS="5"
export DOMOAI_MQTT_USERNAME="domoai"
export DOMOAI_MQTT_PASSWORD="<mqtt-password>"
uv run domoai-mcp
```

Zigbee2MQTT may run alongside Home Assistant or another configured source. The
adapter consumes Zigbee2MQTT bridge/device topics and publishes only mapped
device `/set` commands through the existing plan, policy and executor
boundary. Pairing, removal, OTA, groups, bridge administration and arbitrary
MQTT publishing are not exposed.

## Live Matter Server runtime

The native Matter adapter uses Matter Server as the controller boundary and
connects to its compatible WebSocket endpoint. Configure it alongside Home
Assistant, Zigbee2MQTT or another source:

```bash
export DOMOAI_MATTER_SERVER_URL="ws://matter-server.local:5580/ws"
export DOMOAI_MATTER_TIMEOUT_SECONDS="5"
uv run domoai-mcp
```

The adapter validates the server schema range before discovery, preserves
`node:<node_id>/endpoint:<endpoint_id>` source references and exposes only the
bounded v1 light/switch power and brightness profile plus read-only
temperature, humidity and occupancy state. Commissioning, fabric management,
OTA, groups, vendor clusters and arbitrary attribute operations remain outside
the agent-facing boundary. Live Matter smoke tests are opt-in; fixture tests
need no Matter server or hardware.

## Live KNX/IP runtime

The native KNX adapter uses an explicit mapping file rather than inferring
devices from arbitrary group traffic. Its bounded v1 profile supports light and
switch power, light brightness, and read-only temperature, humidity and
occupancy. Configure it alongside the other physical sources:

```bash
export DOMOAI_KNX_GATEWAY_HOST="knx-gateway.local"
export DOMOAI_KNX_CONFIG_PATH="config/knx.json"
export DOMOAI_KNX_TIMEOUT_SECONDS="5"
uv run domoai-mcp
```

The mapping file declares each entity, semantic capability, state group address,
command group address and DPT. Unknown fields, malformed addresses, unsupported
DPTs and writable sensor mappings are rejected at startup. KNX/IP tunnelling is
optional and can coexist with the other configured adapters; fixture tests use
an in-memory transport and require no gateway or hardware. ETS import,
commissioning, routing, secure credentials, arbitrary group-value operations,
scenes and additional xknx device profiles are not included in v1.

## Live Modbus TCP runtime

The native Modbus adapter uses an explicit v1 mapping of unit IDs, register
areas, zero-based PDU offsets and scalar encodings. It supports light/switch
power, light brightness, and read-only temperature, humidity and occupancy.
Configure it alongside the other physical sources:

```bash
export DOMOAI_MODBUS_HOST="modbus-controller.local"
export DOMOAI_MODBUS_PORT="502"
export DOMOAI_MODBUS_CONFIG_PATH="config/modbus.json"
export DOMOAI_MODBUS_TIMEOUT_SECONDS="5"
export DOMOAI_MODBUS_POLL_INTERVAL_SECONDS="5"
uv run domoai-mcp
```

The mapping is strict and does not scan or infer devices. Unknown fields,
ambiguous `40001`-style addresses, unsupported encodings, writable sensors and
unsafe commands are rejected. Modbus TCP is opt-in and can coexist with Home
Assistant, Zigbee2MQTT, Matter Server and KNX. RTU/ASCII, TLS, scanning, vendor
function codes and arbitrary register reads/writes are outside v1.
Fixture tests use an in-memory transport and require no controller or hardware.

## Multi-adapter identity and routing

The runtime follows the Home Assistant device/entity distinction: one physical
source device may expose multiple source entities, while DomoAI presents one
canonical device with capability-level routes. Stable source identifiers and
connections preserve identity across name or area changes; an explicit
`canonical_id` is required to link contributions from different adapters.
Commands are resolved to one exact source entity before execution. Ambiguous,
unknown or unavailable routes fail closed, so the runtime never silently sends
a command to another protocol or entity.

No live gateway, broker or controller is required for this behavior. The
deterministic multi-adapter fixture covers composition, partial failure,
topology, exact routing and zero-write safety:

```bash
uv run pytest -q tests/contract/test_multi_adapter_runtime.py \
  tests/integration/test_multi_adapter_runtime.py \
  tests/performance/test_multi_adapter_targets.py
```

## Verified local validation

On 2026-08-17 the repository passed the unit, adapter, discovery, plan,
MCP-contract, optimization, performance, Home Assistant execution, KNX and
Modbus fixture, runtime composition, OMIE and Open-Meteo provider scenarios
covered by the repository test suite.
The Home Assistant classic-adapter smoke passed against the local Docker lab;
the local Zigbee2MQTT and Modbus smokes passed; and the read-only OMIE and
Open-Meteo public-network smokes passed with opt-in configuration. Matter
discovery and KNX/IP remain optional because they require a commissioned
Matter node or a reachable KNX gateway and mapping.

The local launch command is:

```bash
uv run domoai-mcp
```

The quality gates are:

```bash
uv run pytest -q
uv run ruff check .
uv run mypy src
uv lock --check
```

The latest full-suite result without live credentials is `972 passed, 10
skipped`, with no warnings. The skips
are opt-in Matter Server, KNX/IP and other live cases without their external
node, gateway or service configuration; deterministic fixture coverage remains
enabled. No live gateway or hardware result is claimed by this run; the Home
Assistant inverter HIL smoke remains opt-in and was not executed because its
credentials and hardware were unavailable. The FastMCP compatibility
seam keeps the known `pydantic_settings` incomplete-field warning out of the
MCP contracts without globally suppressing warnings.

Adapter and public contract guidance lives in [`docs/adapter-sdk.md`](docs/adapter-sdk.md)
and [`docs/contracts.md`](docs/contracts.md).
