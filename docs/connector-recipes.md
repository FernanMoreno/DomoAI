# Declarative connector recipes

> Complete documentation: [`docs/documentation/README.md`](documentation/README.md).

A recipe is a public mapping file. It says which transport is used, how a
candidate is found, which normalized device type and capabilities exist, and
which writes require readback. It never contains a token, password, client
secret or credential-bearing URL.

## Three tiers

1. **Declarative recipe.** Use this when DomoAI already has the transport
   engine and the product's topics, HTTP operations, register map, group
   addresses or Matter interactions are known. MQTT, REST/OpenAPI, Modbus,
   KNX and Matter are supported recipe engines.
2. **Transport plus product profile.** Add a small profile or discovery
   engine when the protocol is supported but the product has unusual
   semantics.
3. **Custom provider and adapter.** Use the five SDK methods and `AdapterPort`
   for proprietary protocols, pairing, binary framing or semantics that cannot
   be represented safely by a recipe.

## Generate one

Create `recipe.yaml` with the public mapping and generate a provider package:

```bash
uv run domoai-admin connector create \
  --recipe ./recipe.yaml \
  --output ./my-connector
uv run domoai-admin connector check ./my-connector
```

The loader uses safe YAML parsing, a size limit and the strict
`ConnectorRecipe` model. The generated package registers one provider entry
point and includes the recipe in its manifest. It reports `incomplete` until
the author has supplied the required runtime engine configuration and tests.

For Modbus, KNX and Matter, the generator also emits the matching engine and
bounded manual-discovery wiring. The deployment still supplies the injected
transport: a YAML file cannot open a socket, commission a Matter fabric or
contain a KNX Secure key. The generated package deliberately remains
`incomplete` until that runtime boundary is wired and qualified.

Secret-free examples are available at:

```text
examples/connectors/modbus-temperature.yaml
examples/connectors/knx-light.yaml
examples/connectors/matter-light.yaml
```

## MQTT example

```yaml
schema_version: v1
connector_id: acme_mqtt
display_name: Acme MQTT
device_type: light
transport_family: mqtt
discovery_strategies:
  - manual
auth_schemes:
  - username_password
capabilities:
  - capability_id: power
    kind: boolean
    state_topic: acme/{device_id}/state
    command:
      name: power
      command_topic: acme/{device_id}/set
      payload_template: '{value}'
      readback_topic: acme/{device_id}/state
```

The MQTT recipe subscribes only to declared state/readback topics. Discovery
must use an exact declared topic; an unbounded `+` or `#` subscription is
rejected. The broker host and credential are supplied through the normal
connection setup and secret store.

## REST/OpenAPI example

```yaml
schema_version: v1
connector_id: acme_rest
display_name: Acme REST
device_type: sensor
transport_family: rest
discovery_strategies:
  - manual
auth_schemes:
  - token
capabilities:
  - capability_id: temperature
    kind: number
    unit: °C
    read_path: /devices/{device_id}
    read_json_path: $.temperature
```

The separate OpenAPI profile must allowlist the `GET` operation and its JSON
response schema. Writable REST mappings add an explicit HTTP method, JSON
payload template, readback operation and readback JSON path. Requests use the
operator-selected HTTPS endpoint, bounded response sizes and no redirects.

## Modbus, KNX and Matter profiles

These profiles are typed declarations compiled into the native DomoAI
adapters. They make every physical interaction explicit:

- Modbus: unit ID, function area, address, data type, codec and readback.
- KNX: group address, DPT, command address and readback address.
- Matter: node allowlist, endpoint, standard cluster/attribute and command
  allowlist.

Unknown capabilities, vendor Matter commands, broadcast KNX address `0/0/0`,
invalid register ranges and writable mappings without readback are rejected
before a transport is constructed. Existing semantic mapping, idempotency,
polling, event and health behavior remains in the native adapter.

The five provider methods are still the custom escape hatch:

```text
discover -> probe -> validate -> build_adapter -> diagnose
```

They are not required for a new product when its behavior fits one of the
recipe engines. They are required for proprietary pairing, serial/binary
framing, BLE/Thread commissioning or vendor semantics that cannot be safely
represented by a typed profile.

## What the agent receives

The flow is:

```text
recipe -> ConnectionProviderPort -> AdapterPort -> DeviceRegistry/StateStore -> MCP
```

`discover` finds connection candidates. The adapter then discovers devices and
maps them to DomoAI's `Device`, `Capability`, `StateSnapshot` and `Command`
models. MCP exposes those normalized semantic objects; it does not expose raw
MQTT topics, arbitrary HTTP paths or vendor-specific tools. An unknown or
ambiguous capability is rejected instead of becoming an executable command.

All activation, credential resolution, replacement, reconnect and detach
operations remain owned by `ConnectionManager` and `ManagedAdapterSet`.

## OAuth deployment preflight

OAuth application registration is external. Check only local readiness with:

```bash
uv run domoai-admin oauth preflight --env-file deploy/gateway.env
```

The command validates the public authorization/token/revoke URLs, client ID,
redirect URI and scopes without network I/O. It reports
`oauth_client_registration_required` until the application and redirect URI
are registered in the provider. Client secrets and tokens remain in the
existing secret store and are never printed by this command.

## Docker verification on WSL

Some Docker Desktop installations expose `credsStore: desktop.exe` in the
WSL Docker configuration without exposing the matching helper binary. Run the
repository checks through the included disposable configuration wrapper:

```bash
./scripts/with_disposable_docker_config.sh \
  project-composition-check "$(cat .ai/project-name)"
```

The wrapper affects only that command and leaves the user's Docker credentials
configuration unchanged.
