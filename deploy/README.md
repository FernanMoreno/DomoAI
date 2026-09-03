# DomoAI universal MCP gateway

This deployment exposes one MCP URL for every MCP-compatible client. Codex,
Claude, Gemini, OpenCode and other hosts use the same Streamable HTTP endpoint;
there is no vendor-specific adapter. The gateway owns one runtime, one
scheduler, one approval store and one physical mutation boundary.

After discovery it writes the secret-free commissioning report to
`/app/data/commissioning-manifest.json` (or the path configured by
`DOMOAI_COMMISSIONING_MANIFEST_PATH`). Agents can read the same report through
the `inspect_commissioning` MCP tool; it never creates actuator authority.

For the local lab, the WSL and Windows launchers load the ignored
`dev/lab/.env` and apply the opt-in `DOMOAI_BOOTSTRAP_PROFILE=lab`. The
allowlisted bootstrap resolves reachable local endpoints, preserves explicit
values, and writes the secret-free `data/runtime-bootstrap.json` manifest. It
does not infer credentials or actuator/HIL authority.

`dev/lab/**` is intentionally not part of this deployment. Home Assistant and
Mosquitto are included in `compose.yaml` for a complete Docker stack. KNX
Virtual and ETS stay on Windows; the WSL `knxd` bridge remains the only client
of KNX Virtual's upstream `172.26.80.1:3671`, while DomoAI uses the separate
bridge endpoint on UDP `3672`.

## Deployment preflight

Before starting the gateway, run the operator-only static gate from the
repository root:

```bash
uv run domoai-admin deployment preflight
```

It validates `deploy/gateway.env`, `deploy/clients.json`, the Compose service
boundary and the Caddy allowlist without starting containers, contacting
Home Assistant/MQTT/KNX, changing databases or touching `dev/lab/**`. It emits
sanitized JSON and exits `0` only when the checks pass. A fresh checkout is
expected to fail until the operator creates the ignored `gateway.env`, client
hash document and host-side configuration files.

After Docker, WSL and the Windows KNX endpoint are deliberately available,
topology checks are opt-in:

```bash
uv run domoai-admin deployment preflight --network
```

This only checks bounded TCP reachability; it does not send KNX group writes,
battery commands or Home Assistant service calls. Real CA/DNS/firewall setup,
credentials and hardware qualification remain separate deployment gates.

## Native or WSL

```bash
cp deploy/gateway.env.example deploy/gateway.env
# Create deploy/clients.json and replace every placeholder secret.
set -a
. deploy/gateway.env
set +a
DOMOAI_MCP_CLIENT_TOKEN_FILE=deploy/clients.json \
  uv run --project . domoai-mcp-gateway
```

For a native local-only process, bind `DOMOAI_MCP_HOST=127.0.0.1` and use an
`http://127.0.0.1:<port>` public URL. A non-loopback bind requires a client
token file and an HTTPS public URL. `deploy/wsl/run-gateway.sh` and
`deploy/windows/run-gateway.ps1` are equivalent launchers.

## Docker with Home Assistant, MQTT and external KNX

```bash
cp deploy/gateway.env.example deploy/gateway.env
# Create deploy/clients.json; set DOMOAI_HOME_ASSISTANT_TOKEN after onboarding HA.
docker compose --env-file deploy/gateway.env -f deploy/compose.yaml up -d --build
docker compose --env-file deploy/gateway.env -f deploy/compose.yaml ps
curl -k https://mcp.example.test/readyz
```

The gateway container reaches Home Assistant as `homeassistant:8123` and MQTT
as `mqtt:1883`. It reaches KNX through `host.docker.internal:3672` by default;
set `DOMOAI_KNX_GATEWAY_HOST` to the actual WSL address if Docker Desktop does
not route that name to the WSL bridge. Never point DomoAI at KNX Virtual's
`3671` while `knxd` owns that upstream session.

The gateway uses Streamable HTTP JSON responses by default
(`DOMOAI_MCP_JSON_RESPONSE=true`). It answers the optional server-to-client GET
stream with `405 Method Not Allowed` unless
`DOMOAI_MCP_SERVER_SENT_EVENTS=true` is explicitly enabled. This keeps the
normal agent/tool path bounded with the pinned MCP SDK; enable SSE only after
qualifying the deployment's dependency lifecycle.

The token file contains only SHA-256 hashes and server-owned scopes:

```json
{
  "clients": [
    {"client_id": "codex", "token_hash": "<64 hex chars>", "scopes": ["read", "mutate"]}
  ]
}
```

Keep the raw bearer token in the client secret store, never in Git, tool
arguments or audit events. A client with `read` may inspect and propose; the
`mutate` scope is required for approval requests, scheduling and execution.
Human approval remains a separate trusted-host assertion.

## TLS and network boundary

Compose runs Caddy as the only host-published edge. The gateway port 8124 is
internal to the Compose network, while Home Assistant and MQTT are bound to
localhost for operator access only. Their host ports default to 8123 and 1883
and can be changed with `DOMOAI_HOME_ASSISTANT_HOST_PORT` and
`DOMOAI_MQTT_HOST_PORT` when the virtual lab already owns those ports; the
bind remains loopback-only. The example Caddyfile forwards only MCP and health
routes and uses an internal certificate for a private network.
Replace it with a certificate issued by the deployment's trusted CA when
clients do not trust Caddy's internal CA. Restrict firewall access to the
proxy and never expose KNXnet/IP, Home Assistant or MQTT directly to the
Internet.

The hostname is controlled by DOMOAI_CADDY_HOSTNAME and must agree with
DOMOAI_MCP_PUBLIC_URL. The default internal certificate is suitable only for a
private lab network; external agents need a trusted certificate/CA and a
firewall policy. Readiness remains an application signal: the proxy healthcheck
uses /healthz so a temporary adapter/readiness failure does not create a proxy
restart loop. /readyz is still proxied unchanged and may return 503.

## Client configuration

Configure each MCP host with the same URL and its own bearer token:

```json
{
  "url": "https://mcp.example.test/mcp",
  "headers": {"Authorization": "Bearer <client-secret>"}
}
```

The exact UI/CLI field names vary by client, but the MCP transport contract is
the same. The gateway does not need to know whether the caller is Codex,
Claude, Gemini or OpenCode.

To verify any host before granting it mutation scope, use the common read-only
probe. Supply the bearer through the environment or stdin, never as an
argument:

```bash
DOMOAI_MCP_PROBE_URL=https://mcp.example.test/mcp \
DOMOAI_MCP_PROBE_TOKEN="<secret-store-value>" \
  uv run domoai-mcp-probe --client-label codex
```

Repeat with the deployment-owned token for `claude`, `opencode`, `gemini`, or
another MCP-compatible host. The output is canonical JSON containing only
catalog/runtime/discovery digests and never the bearer or token hash. The
complete common contract is in
`specs/172-multi-agent-mcp-operation/quickstart.md`.

Readiness is intentionally stricter than liveness: `/healthz` proves the
process responds, while `/readyz` requires the runtime lifecycle and adapter
health to be active. An ownership conflict or adapter degradation keeps the
gateway out of ready state and does not trigger an automatic takeover.

### Recovering a stale runtime owner

If startup reports an ownership conflict, first verify that no gateway process
is running and that no process is listening on the configured gateway port.
Do not delete the SQLite database, `-wal`/`-shm` files or the ownership row.
Read the recorded owner ID from the deployment database, then run the
operator-only recovery command with that exact ID while the gateway is
stopped:

```bash
uv run domoai-admin runtime release-stale-owner \
  --database data/domoai.sqlite3 \
  --deployment-id default \
  --owner-id <recorded-owner-id>
```

The command takes the same non-blocking SQLite advisory lock used by the
gateway. It refuses with `runtime_owner_active` if a live owner exists and
with `runtime_owner_mismatch` if the recorded ID changed, so it cannot take
over a newer process accidentally. Releasing this durable record is only an
administrative recovery operation; it is not evidence that a latched actuator
is stopped. On the next startup the normal plan recovery and physical control
reconciliation still run. For Docker, execute the same command in the
maintenance container with the database path `/app/data/domoai.sqlite3`.

## Backup and restore

The runtime data volume is durable, but it is not itself a backup. Use a
protected host directory outside `domoai-data`; the administrative command
captures the operational and audit SQLite databases with SQLite's online
backup API and writes a digest/integrity manifest. It never requires copying
`-wal` or `-shm` sidecars.

For Docker, create the backup while the gateway is running with a one-shot
maintenance container. The host directory must be outside `/app/data`:

```bash
mkdir -p ./backups
docker compose -f deploy/compose.yaml run --rm \
  -v "$(pwd)/backups:/var/backups/domoai" \
  gateway domoai-admin backup create \
  --database /app/data/domoai.sqlite3 \
  --audit-database /app/data/domoai-audit.sqlite3 \
  --output-dir /var/backups/domoai \
  --deployment-id home-main
```

Verify a published set before relying on it:

```bash
docker compose -f deploy/compose.yaml run --rm \
  -v "$(pwd)/backups:/var/backups/domoai:ro" \
  gateway domoai-admin backup verify \
  --backup-dir /var/backups/domoai/<backup-id>
```

Restore is an offline administrative operation. Stop the gateway first; the
command also refuses a target with active or uncertain runtime ownership:

```bash
docker compose -f deploy/compose.yaml stop gateway
docker compose -f deploy/compose.yaml run --rm \
  -v "$(pwd)/backups:/var/backups/domoai:ro" \
  gateway domoai-admin backup restore \
  --backup-dir /var/backups/domoai/<backup-id> \
  --target-data-dir /app/data \
  --deployment-id home-main
docker compose -f deploy/compose.yaml start gateway
```

The restore validates both members, stages/migrates the copy, keeps a local
rollback directory when replacing existing data, and does not execute plans or
replay adapter writes. Keep a small rolling set (for example, seven copies),
restrict the backup directory to the service operator, and test verification
regularly. Encryption, off-site replication and cloud retention are not yet
provided by this command and must be supplied by the deployment environment.

For native Linux/WSL, use the same `domoai-admin` commands with paths on a
protected filesystem. Do not place `--output-dir` below the live database's
parent directory and do not run restore while the gateway process is active.

### KNX Virtual on Windows with WSL `knxd`

When KNX Virtual/ETS runs on Windows and `knxd` runs in the same WSL instance,
keep the two network hops explicit. `knxd` connects upstream to Windows through
WSL loopback, while DomoAI connects downstream to the local `knxd` tunnel:

```bash
DOMOAI_KNX_KV_HOST=127.0.0.1 \
  bash dev/lab/knx-gateway/run-wsl.sh

DOMOAI_KNX_GATEWAY_HOST=127.0.0.1 \
  bash deploy/wsl/run-gateway.sh
```

ETS/KNX Virtual still uses its Windows-side KNXnet/IP port `3671`; the WSL
bridge remains on `3672`. Do not reuse an old WSL address such as
`172.26.80.1` or `172.26.93.253` after a networking-mode change. If the
gateway starts before `knxd`, restart the gateway after `knxd` is listening so
the initial KNX discovery runs against the live tunnel. A connected tunnel is
not sufficient for readiness: configured group addresses must also provide
valid readback, and a lab/software-qualified battery must remain below
production `/readyz` qualification by design.
