# Unified MCP gateway

The supported integration boundary for external agents is the shared gateway:

```text
Codex / Claude / Gemini / OpenCode / any MCP client
                         │
                         ▼
              https://host.example/mcp
                         │
                         ▼
                 one DomoAI runtime
```

The gateway exposes the existing semantic catalog over MCP Streamable HTTP.
Local stdio remains available through `domoai-mcp` for development and
backwards compatibility. `domoai-mcp-gateway` is the long-lived network
entrypoint and fails closed when no concrete provider is configured; it never
silently selects the deterministic simulator. The simulator is available only
through the explicit local fixture entrypoint.

The gateway remains protocol-neutral: support is determined by internal
connectors loaded behind the one universal semantic adapter, never by a
vendor-specific MCP tool or public adapter. The current native-connector,
Home-Assistant-mediated, internal-extension, fixture and unavailable coverage
is maintained in [`adapter-coverage.md`](adapter-coverage.md). That inventory
is descriptive; it does not grant physical authority or claim a lab
qualification.
Generic MQTT uses the same semantic catalog and plan lifecycle as every other
adapter; it does not add an MQTT-specific MCP operation.

## Contract

- MCP endpoint: `POST` and `GET` at the configured `DOMOAI_MCP_PATH` (default
  `/mcp`). The optional server-to-client `GET` stream returns `405` by default
  because DomoAI does not emit unsolicited messages. Set
  `DOMOAI_MCP_SERVER_SENT_EVENTS=true` only after qualifying SSE lifecycle.
- Authentication: `Authorization: Bearer <client-secret>` when a token file is
  configured; non-loopback deployments require both a token file and HTTPS.
- Authentication profile: DomoAI currently supports provisioned static bearer
  tokens whose SHA-256 hashes live in the server-owned token file. This profile
  does not implement an OAuth authorization server, login, consent screen or
  token endpoint. Clients that require OAuth discovery/login need an external
  authorization layer.
- Token records are server-owned and contain a client ID, hash, scopes,
  enabled flag, creation/expiry/revocation timestamps and authority scope.
  Raw tokens never enter DomoAI responses, persisted records or audit payloads.
- `read` is sufficient for discovery, state and preview tools. `mutate` is
  required for prepare, approval requests, execution, scheduling and
  cancellation. `preview_*` never persists a plan; `prepare_*` does.
- Verified token claims are the only source of MCP principal, role, tenant,
  household, area, device, capability and operation scope. The request body
  cannot elevate itself by supplying an owner or another principal. Viewer,
  planner and operator roles are separated, and list/resource responses are
  filtered to the authenticated scope.
- Agent authentication is not human approval. Sensitive plans still require a
  trusted operator assertion and a scoped, expiring, one-shot approval grant.
- `/healthz` is unauthenticated liveness. `/readyz` is unauthenticated but
  fail-closed readiness and contains only sanitized runtime/adapter status.
- `domotics://runtime` is an authenticated read-only deployment matrix. It
  reports active provider IDs, writable capability routes and the configured
  authority state without exposing secrets or granting execution authority.
- `domotics://coverage` reports the semantic operation set for every discovered
  capability: readable/writable flags, recognized commands, units/limits and
  confirmation guarantees. Missing routes remain unavailable and never become
  executable merely because a protocol is present.
- `domotics://coverage-evidence` is a separate, typed v1 inspection resource.
  It groups visible canonical device/capability rows, shows each route's source
  availability and current runtime membership, and lists gaps for lighting,
  metered plugs, covers, HVAC and schedulable loads. Evidence remains
  `unknown` / `not_verified`; protocol, profile, firmware, observed identity
  and test dates are null. Declared guarantees and imported recipes are not
  hardware qualification. Route availability grants no execution authority.
  Consumers should use this single response rather than combine resources
  assuming that a shared inventory revision freezes operational availability.
  The legacy coverage response is unchanged. See
  [`coverage-evidence.schema.json`](../schemas/v1/coverage-evidence.schema.json).
- `inspect_commissioning` and `domotics://commissioning` are authenticated
  read-only views of the same sanitized battery/EV commissioning report. A
  candidate is preparation evidence only; it cannot create a binding, lease,
  approval or qualification.
- `verify_commissioning` verifies owner-attributed evidence against that report
  without calling an adapter or creating physical authority. Simulation and
  unavailable KNX/ETS/knxd or HIL dependencies remain explicitly blocked.
- `summarize_solution` is a deterministic, read-only product projection of an
  optimization result. It contains no executable command list; plan validation,
  policy, approval and admission remain separate.
- `compare_scenarios` is a bounded, read-only counterfactual projection. It
  returns baseline/variation summaries and objective diffs; an infeasible
  baseline stops the variations and no diff is fabricated.
- `execute_scene` is available only when the configured runtime owns the bundle
  boundary. It accepts a digest-bound ordered scene and delegates every member
  to validation, approval, admission, execution and physical readback; it is
  not a second executor.
- The configured builder additionally publishes `export_household_data` and
  `delete_household_data`. They are household-scoped, owner/service-authorized,
  redact credential-shaped keys, and preserve immutable audit records. The
  retention policy is controlled by `DOMOAI_PRIVACY_RETENTION_DAYS`. Exports
  return bounded pages with `total_record_count` and `next_cursor`; the cursor
  is signed for the authorized household and category selection.

## One runtime and one authority

All clients connect to the same process and therefore share registry, state,
scheduler, optimizer workers, durable plans, approval grants and adapter
connections. SQLite persists a deployment-scoped ownership record. A second
process for the same `DOMOAI_MCP_DEPLOYMENT_ID` fails before adapter connection;
there is no automatic takeover after an uncertain owner.

The network server and stdio compatibility path use the same server builder and
the same lifecycle owner. No MCP client can call an adapter directly.

The client-neutral prompt catalog contains `discover-domotics-inventory`,
`diagnose-domotics-device` and `prepare-energy-plan`. Prompts are guidance only:
they return procedures and semantic tool/resource names, never plan IDs,
approval grants, adapter calls or execution authority. The read-only
`domotics://coverage` resource reports active, unavailable and indirect routes
from the same registry used by the runtime.

If a gateway exits without clearing its durable owner record, recovery is an
offline operator action, never an MCP operation. After verifying that no live
gateway holds the port, run `domoai-admin runtime release-stale-owner` with
the exact recorded deployment and owner IDs. The command acquires the same
SQLite advisory lock non-blocking and refuses both live ownership and owner-ID
mismatch; it never deletes data or takes over a live runtime. Startup then
performs the normal recovery and actuator-control reconciliation.

For the Docker deployment, Caddy is the only host-published MCP edge. The
gateway listens on 8124 only on the private Compose network; clients use the
HTTPS proxy URL and /mcp path. Home Assistant and MQTT are localhost-bound
operator services, not public MCP routes. /healthz is the edge liveness probe;
/readyz is forwarded unchanged and can remain 503 when the runtime is not
ready.

The configured builder validates the client-token file before opening runtime
resources. Local fixture construction is intentionally separate and must be
requested explicitly by development/test code.

## Cross-platform topology

Home Assistant and MQTT can run in Docker, the gateway can run natively in
Linux/WSL/Windows or in the provided Docker stack, and KNX Virtual/ETS can stay
outside Docker on Windows. For the KNX lab topology, KNX Virtual keeps its
upstream KNXnet/IP endpoint on UDP/3671; the address depends on WSL networking
mode. In the current mirrored WSL setup, it is reachable as
`127.0.0.1:3671`; in classic NAT mode use the Windows-side address reachable
from WSL. WSL `knxd` publishes the separate DomoAI/ETS endpoint on `3672`.
Configure the gateway with the reachable WSL address and
`DOMOAI_KNX_GATEWAY_PORT=3672`.

Kubernetes is not required for one home/deployment. Active-active replicas are
intentionally unsupported until an external fencing and actuator ownership
protocol exists.

## Identity and token lifecycle

Use the offline administrator boundary to rotate or revoke a client. Rotation
returns the new bearer once on stdout; the token file stores only its SHA-256
hash and is written with owner-only permissions:

```bash
uv run domoai-admin tokens rotate \
  --file deploy/clients.json \
  --client-id codex \
  --scopes read,mutate \
  --tenant-id tenant-main \
  --households home-main \
  --roles operator
uv run domoai-admin tokens revoke \
  --file deploy/clients.json \
  --client-id codex
```

Plans, approvals, bundles, schedules, state snapshots, local automations and
audit events carry the non-secret authority context. Existing v1 JSON rows
without that field load as the default local household and are upgraded on
their next write; this is a compatibility path, not a cross-household grant.

## Encrypted backups

Set `DOMOAI_BACKUP_ENCRYPTION_KEY_FILE` to a separate 32-byte file with mode
`0600`. Backup format v2 encrypts each SQLite member with AES-256-GCM; the key
must not live inside the backup directory:

```bash
domoai-admin backup create ... --encryption-key-file /run/secrets/domoai-backup.key
domoai-admin backup verify ... --encryption-key-file /run/secrets/domoai-backup.key
domoai-admin backup restore ... --encryption-key-file /run/secrets/domoai-backup.key
```

Verification authenticates and decrypts into temporary staging before restore
replacement. Wrong keys, tampering and unknown formats fail before the target
directory changes. Unencrypted v1 local backups remain readable explicitly for
backward compatibility.

Federation is currently a signed, expiring, idempotent proposal boundary. A
valid proposal is recorded as `accepted` only after target tenant/household
policy checks; it never invokes an adapter and never substitutes for local
human approval. An optional bearer-protected Prometheus pull endpoint is
available at `/metrics`. The runtime now carries a bounded `instance_id`,
fencing counters, per-household queue depth and additive local metric history.
Set `DOMOAI_MULTI_HOST_ENABLED=true` only when the composition root injects a
real external lease coordinator; normal settings fail closed without it. The
deterministic coordinator is test-only. Active-active workers, remote
aggregation and production failover remain blocked until provider
qualification. See
[`evaluacion-coordinacion-distribuida-pools-metricas.md`](evaluacion-coordinacion-distribuida-pools-metricas.md).
## Coordinación multi-host

El runtime single-writer sigue siendo la configuración por defecto. Para el
modo multi-host aprobado, etcd es la autoridad de leases/fencing y PostgreSQL
es el control plane compartido; la activación requiere mTLS, quorum externo,
adapter fencing-aware y gateway físico que rechace epochs antiguos. La guía
operativa está en [`deploy/multihost/README.md`](../deploy/multihost/README.md).

Active-active no forma parte del contrato habilitado.

### Runtime contract compatibility (package 253)

Plans without deployment or execution-lease metadata retain the v1 contract. A
Plan carrying either field uses v2; its configured runtime binds deployment and
version before validation or consent. AutomationRule follows its template
version, and OptimizationResult uses v2 when either `plan` or any `plans` member
uses v2. An empty result remains v1. Explicit contradictory labels are rejected.
The five historical v1 artifacts remain byte-identical. Select the corresponding
v2 Plan, AutomationRule, OptimizationResult, AutomationEvent or PhysicalIntent
schema for new shapes; EnergyContext and OptimizationScenario keep their
independent A09 version selection.

State events omit an absent target. Time events require `target_rule_id` and
cannot replay an old broadcast event. Physical intent owner, attempt and lease
metadata remain present when recorded; an absent optional identity is omitted.
Neither internal type gains an in-band version field.

Active Plan lookup rechecks durable compatibility even when a cached object
exists. An incompatible stored definition whose scope has not been verified is
unavailable through active lookup, with the same public result as an absent
ID. Historical classification is exposed only through authorized diagnostics
or scoped offline administration. They cannot be executed, approved, scheduled or bundled by
passing a historical wrapper. Existing scheduled-plan and local-rule lists keep
compatible entries and add `compatibility: {records, next_offset}`. Each record
contains only its kind, safe ID and reason. `compatibility_offset` selects the
next diagnostic page; tenant, household and deployment are filtered before
pagination. No public history getter is added.

The offline `domoai-admin runtime-contract history --record-kind plan
--record-id ID` command uses the configured administrative database and scope.
It returns sanitized original evidence in a wrapper with `executable: false`;
it never reconstructs a current Plan or reissues consent. Use
`domoai-admin runtime-contract preflight --limit 100 --offset 0` to inventory
compatibility blocks. Explicit revalidation creates a new definition through
normal validation and consent; retain the old identity and approval evidence.

For device-, area- or capability-restricted readers, compatible lists keep
their normal filtering, but the compatibility inventory is not queried. Its
constant response is `{"status":"unavailable","reason":"whole_household_authority_required","records":[],"next_offset":null}`.
This means inventory unavailable, not no affected records. Full-household
authority retains the scoped diagnostic pages described above.

Bundle receipt retries also require current authority over every canonical member.
An authorized retry retains its existing terminal result without dispatching again;
revoked access or a foreign member tenant cannot reveal the stored receipt.
Historical incompatible members produce the same unavailable response as absent IDs.

Canonical scheduled plans and rules are also filtered by their stored tenant,
even when a different tenant uses the same household and device identifiers.
