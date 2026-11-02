# Connection Provider SDK v1

> Complete connector documentation: [`docs/documentation/README.md`](documentation/README.md).

DomoAI connection providers implement the contract in
`domoai.runtime.connection_ports.ConnectionProviderPort` and expose a strict
`ConnectionProviderManifest`. The manifest is non-secret metadata; credentials
are passed only through opaque `secret:`/`env:` references in the existing
connection setup flow.

## Entry point

Third-party packages register a factory in the Python entry-point group:

```toml
[project.entry-points."domoai.connection_providers"]
my_connector = "my_package.provider:create_provider"
```

The factory returns a provider instance. The provider must expose async
`discover`, `probe`, `validate`, `build_adapter` and `diagnose` methods, and
must return candidates/diagnostics whose connector provenance matches its
manifest. A broken entry point is isolated and reported; it does not prevent
valid providers or zero-provider boot.

## Safety boundary

Provider metadata and diagnostics must not contain passwords, tokens, complete
endpoint credentials, or sensitive serials. Providers do not publish commands
or bypass `ConnectionManager`, `ManagedAdapterSet` or `AdapterPort`.

The loader is available as `ConnectionProviderLoader.load(...)` for tests and
as `ConnectionProviderRegistry.from_entry_points(...)` for composition-root
integration.

## Create a connector without boilerplate

The repository includes an authoring command. It creates the package, entry
point, provider class, adapter boundary and a starter contract test:

```bash
uv run domoai-admin connector create \
  --connector-id my_connector \
  --display-name "My Connector" \
  --protocol my-protocol \
  --output ./my-connector
uv run domoai-admin connector check ./my-connector
```

The generated provider explicitly reports `provider_adapter_not_implemented`
until its protocol-specific `discover`, `probe`, `validate`, `build_adapter`
and `diagnose` behavior is completed. The common
`ConnectionProviderBase` supplies safe defaults and a deterministic
`manual_candidate`; it never pretends that an unimplemented adapter is ready.

Provider setup forms are described by secret-free `SetupFieldDescriptor`
metadata in the manifest. The management UI reads
`GET /api/v1/connection-providers`, so a newly installed connector does not
require a new provider-specific page. Raw credentials are still accepted only
by the narrow create/reauth operation and are stored through the configured
secret store.

## Declarative recipes

When the transport engine already exists, a provider can be generated from a
strict public `ConnectorRecipe` instead of starting with five handwritten
methods:

```bash
uv run domoai-admin connector create --recipe ./recipe.yaml --output ./my-connector
uv run domoai-admin connector check ./my-connector
```

Recipes support bounded MQTT, REST/OpenAPI, Modbus, KNX and Matter engines.
They describe `transport_family`, `device_type`, discovery/authentication
schemes and normalized capability bindings. MQTT topics and REST paths accept
only the `{device_id}` variable; writes accept `{value}` and require an
explicit readback. OpenAPI operations are separately allowlisted and their
JSON response schemas are required. Modbus, KNX and Matter use typed profiles
for register codecs, group addresses/DPTs and standard endpoint/cluster/
attribute interactions. See
[`docs/connector-recipes.md`](connector-recipes.md) for examples and the
custom-provider boundary.

The recipe is public metadata. Broker/API endpoints are selected during
onboarding, while tokens and passwords stay in the configured secret store.
The generated project deliberately reports `incomplete` until its deployment
engine and contract tests are supplied. Once active, its adapter still
returns only the normalized `AdapterPort` surface used by the agent.

This is why a new product does not normally require implementing the five
methods. If its behavior fits a supported recipe, the generator supplies the
provider lifecycle and the deployment injects the real transport. A custom
provider remains necessary for proprietary discovery, pairing, serial/BLE/
Thread flows or semantics outside the typed allowlists.

## P2 operator workflows

The local management API exposes the reviewed P2 boundaries:

- `POST /api/v1/knx/ets/preview` and `/persist` import bounded XML or `.knxproj`
  sources; persistence requires the exact preview digest and explicit
  confirmation.
- KNX Secure accepts `ip_secure`, `data_secure` and `ip_and_data_secure`. The
  provider resolves credential references only while constructing xknx and
  never falls back to ordinary tunnelling. `secure_interface`, when set, is
  the local bind IP passed to xknx.
- Home Assistant OAuth is an external Authorization Code + PKCE client. It
  stores token bundles in `FileSecretStore`; responses expose only an opaque
  reference and expiry metadata. DomoAI is not an OAuth authorization server.
- Home Assistant OAuth setup can call `POST /api/v1/home-assistant/oauth/discover`
  with a base URL. DomoAI tries only the standard OAuth authorization-server
  and OpenID configuration locations, then returns a validated manual fallback.
  Client ID and redirect URI remain application registration inputs; they are
  bound to the one-time authorization state so the callback needs only state
  and code.
- Repair plans are generated from a fixed diagnostic catalog. `retry` is the
  only non-confirmed action; endpoint, interface, mapping, profile, trust and
  credential changes require an explicit confirmation and are delegated to
  `ConnectionManager`.

The P2 routes share the management API's authentication, origin and rate-limit
middleware. Secret values must be supplied only to the narrow operation that
stores or rotates them; they must not be placed in endpoint/options metadata,
repair parameters, logs or audit payloads.

OAuth deployment readiness is checked offline with
`uv run domoai-admin oauth preflight --env-file deploy/gateway.env`. This
validates public configuration and reports the external client-registration
gate; it does not register an application or exchange a token.
