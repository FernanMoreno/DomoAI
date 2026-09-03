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

## Contract

- MCP endpoint: `POST` and `GET` at the configured `DOMOAI_MCP_PATH` (default
  `/mcp`). The optional server-to-client `GET` stream returns `405` by default
  because DomoAI does not emit unsolicited messages. Set
  `DOMOAI_MCP_SERVER_SENT_EVENTS=true` only after qualifying SSE lifecycle.
- Authentication: `Authorization: Bearer <client-secret>` when a token file is
  configured; non-loopback deployments require both a token file and HTTPS.
- Token records are server-owned and contain a client ID, hash, scopes,
  enabled flag and optional expiry. Raw tokens never enter DomoAI responses or
  audit payloads.
- `read` is sufficient for discovery, state and proposal tools. `mutate` is
  required for approval requests, execution, scheduling and cancellation.
- Agent authentication is not human approval. Sensitive plans still require a
  trusted operator assertion and a scoped, expiring, one-shot approval grant.
- `/healthz` is unauthenticated liveness. `/readyz` is unauthenticated but
  fail-closed readiness and contains only sanitized runtime/adapter status.
- `domotics://runtime` is an authenticated read-only deployment matrix. It
  reports active provider IDs, writable capability routes and the configured
  authority state without exposing secrets or granting execution authority.
- `inspect_commissioning` and `domotics://commissioning` are authenticated
  read-only views of the same sanitized battery/EV commissioning report. A
  candidate is preparation evidence only; it cannot create a binding, lease,
  approval or qualification.

## One runtime and one authority

All clients connect to the same process and therefore share registry, state,
scheduler, optimizer workers, durable plans, approval grants and adapter
connections. SQLite persists a deployment-scoped ownership record. A second
process for the same `DOMOAI_MCP_DEPLOYMENT_ID` fails before adapter connection;
there is no automatic takeover after an uncertain owner.

The network server and stdio compatibility path use the same server builder and
the same lifecycle owner. No MCP client can call an adapter directly.

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
upstream `172.26.80.1:3671`; WSL `knxd` publishes the separate DomoAI/ETS
endpoint on `3672`. Configure the gateway with the reachable WSL address and
`DOMOAI_KNX_GATEWAY_PORT=3672`.

Kubernetes is not required for one home/deployment. Active-active replicas are
intentionally unsupported until an external fencing and actuator ownership
protocol exists.
