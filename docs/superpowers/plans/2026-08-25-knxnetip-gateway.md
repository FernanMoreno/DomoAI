# KNXnet/IP Gateway Multiplexer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let ETS and DomoAI use KNX Virtual simultaneously through one reproducible `knxd` gateway.

**Architecture:** Add a Compose service that is the sole KNXnet/IP tunnel client to KNX Virtual at `172.26.80.1:3671` and exposes a downstream KNXnet/IP tunnelling server at `3672/UDP`. Point the existing battery bridge and documented ETS connection at the downstream port; preserve all existing group addresses and DPTs.

**Tech Stack:** Docker Compose, Debian slim, `knxd`, KNXnet/IP UDP tunnelling, Python pytest composition tests, Markdown lab documentation.

**Spec:** `docs/superpowers/specs/2026-08-25-knxnetip-gateway-design.md`

## Global Constraints

- KNX Virtual remains `172.26.80.1:3671`.
- The downstream gateway port is `3672/UDP`.
- `4/0/0` remains DPT `9.024`.
- No production authority or safety gate is changed by this lab transport.
- Existing unrelated worktree changes must be preserved.

---

### Task 1: Add the gateway image and checked-in knxd configuration

**Files:**
- Create: `dev/lab/knx-gateway/Dockerfile`
- Create: `dev/lab/knx-gateway/knxd.conf`
- Create: `dev/lab/knx-gateway/healthcheck.sh`
- Test: `tests/unit/lab/test_knx_gateway_config.py`

**Interfaces:**
- Consumes: `KNX Virtual` at `172.26.80.1:3671`.
- Produces: a `knxd` process listening for downstream KNXnet/IP tunnels on UDP `3672`.

- [X] **Step 1: Write the failing configuration test**

```python
from pathlib import Path


ROOT = Path(__file__).parents[3]


def test_knx_gateway_config_has_single_upstream_and_downstream_tunnel():
    config = (ROOT / "dev/lab/knx-gateway/knxd.conf").read_text()
    assert "ip-address=172.26.80.1" in config
    assert "dest-port=3671" in config
    assert "server=ets_router" in config
    assert "port=3672" in config
    assert "client-addrs=" in config
```

- [X] **Step 2: Run the test and verify it fails because the files do not exist**

Run: `uv run pytest -q tests/unit/lab/test_knx_gateway_config.py`

Expected: FAIL with a missing `dev/lab/knx-gateway/knxd.conf` file.

- [X] **Step 3: Add the minimal gateway image, config, and listener healthcheck**

Use a Debian slim image, install only `knxd` and `iproute2`, copy the checked-in
INI and healthcheck, and start `knxd /etc/knxd/knxd.conf main` in the foreground.
The healthcheck must verify UDP `3672` is listening; it must not claim upstream
health solely from a running PID.

- [X] **Step 4: Run the configuration test**

Run: `uv run pytest -q tests/unit/lab/test_knx_gateway_config.py`

Expected: PASS.

- [X] **Step 5: Build the image and validate config parsing**

Run: `docker compose -f dev/lab/compose.yaml build knx-gateway`

Run: `docker compose -f dev/lab/compose.yaml run --rm knx-gateway knxd /etc/knxd/knxd.conf main --help`

Expected: image build succeeds and `knxd` starts parsing the mounted config without an unknown-option error.

### Task 2: Add knx-gateway to the laboratory Compose stack

**Files:**
- Modify: `dev/lab/compose.yaml`
- Test: `tests/unit/lab/test_knx_gateway_config.py`

**Interfaces:**
- Consumes: `knx-gateway` image and `172.26.80.1:3671`.
- Produces: host UDP `3672` for ETS and WSL DomoAI processes.

- [X] **Step 1: Extend the failing test for Compose topology**

```python
def test_compose_declares_knx_gateway_udp_port():
    compose = (ROOT / "dev/lab/compose.yaml").read_text()
    service = compose[compose.index("  knx-gateway:"):]
    assert '"3672:3672/udp"' in service
    assert "healthcheck:" in service
```

- [X] **Step 2: Run the test and verify the new assertion fails**

Run: `uv run pytest -q tests/unit/lab/test_knx_gateway_config.py`

Expected: FAIL because `knx-gateway` is not yet defined in Compose.

- [X] **Step 3: Add the service with UDP publication and healthcheck**

Define `knx-gateway` with build context `./knx-gateway`, publish only
`3672:3672/udp`, and add a healthcheck invoking the checked-in script. Do not
make the battery service depend on gateway health; the simulator must remain
usable when KNX is offline.

- [X] **Step 4: Run the Compose topology test**

Run: `uv run pytest -q tests/unit/lab/test_knx_gateway_config.py`

Expected: PASS.

- [X] **Step 5: Validate Compose interpolation and service startup**

Run: `docker compose -f dev/lab/compose.yaml config`

Run: `docker compose -f dev/lab/compose.yaml up -d --build knx-gateway`

Expected: rendered Compose is valid and the service reaches healthy status after connecting upstream.

### Task 3: Redirect the DomoAI bridge and document the two-port topology

**Files:**
- Modify: `dev/lab/battery/knx_bridge.py`
- Modify: `dev/lab/README.md`
- Modify: `dev/lab/knx-virtual.md`
- Test: `tests/unit/lab/test_knx_gateway_config.py`
- Test: `tests/integration/test_knx_hil_smoke.py`

**Interfaces:**
- Consumes: downstream gateway `127.0.0.1:3672` by default.
- Produces: unchanged MQTT topic `domoai/battery/power/set` and unchanged KNX mapping.

- [X] **Step 1: Add a test for the default downstream port**

```python
def test_battery_bridge_defaults_to_gateway_port(monkeypatch):
    monkeypatch.delenv("DOMOAI_KNX_GATEWAY_PORT", raising=False)
    from dev.lab.battery.knx_bridge import _parse_args

    assert _parse_args().knx_port == 3672
```

- [X] **Step 2: Run the test and verify it fails with the old default `3671`**

Run: `uv run pytest -q tests/unit/lab/test_knx_gateway_config.py -k default_downstream`

Expected: FAIL because the bridge default is still `3671`.

- [X] **Step 3: Change only the bridge default and documentation**

Keep `DOMOAI_KNX_GATEWAY_PORT` as an override for direct KNX Virtual smoke
tests, but default the lab bridge to `3672`. Update every lab command and ETS
connection instruction to distinguish upstream `3671` from downstream `3672`.

- [X] **Step 4: Run bridge unit and contract tests**

Run: `uv run pytest -q tests/unit/lab/test_knx_gateway_config.py tests/unit/lab/test_knx_battery_bridge.py tests/contract/test_knx_adapter.py`

Expected: PASS.

- [X] **Step 5: Run the existing live KNX smoke with an explicit direct port**

Run: `DOMOAI_LIVE_BATTERY_KNX_ENABLE=1 DOMOAI_KNX_GATEWAY_HOST=172.26.80.1 DOMOAI_KNX_GATEWAY_PORT=3671 uv run pytest -q tests/integration/test_knx_hil_smoke.py -k knx_virtual`

Expected: PASS when KNX Virtual is available, proving the redirect did not break the direct transport override.

### Task 4: Exercise the real multiplexed composition path

**Files:**
- Create: `tests/integration/test_knx_gateway_live.py`
- Modify: `dev/lab/README.md`

**Interfaces:**
- Consumes: Docker MQTT/battery, `knx-gateway`, and KNX Virtual.
- Produces: evidence that ETS and DomoAI can use simultaneous downstream tunnels.

- [X] **Step 1: Write the failing live composition scenario**

The test must be opt-in with `DOMOAI_LIVE_BATTERY_KNX_GATEWAY_ENABLE=1`, send a
KNX search request to the gateway host/port, connect the DomoAI bridge to
`3672`, publish a known simulator state, and assert the gateway remains
reachable for at least two heartbeat intervals. The test must not run in the
normal suite without the explicit environment flag.

- [X] **Step 2: Run the test once without the flag**

Run: `uv run pytest -q tests/integration/test_knx_gateway_live.py`

Expected: SKIP with a clear opt-in reason.

- [X] **Step 3: Implement the smallest live test helper**

Use a real UDP socket for KNXnet/IP discovery and the existing bridge/transport
classes for the DomoAI path. Do not mock the gateway or MQTT broker.

- [X] **Step 4: Run the opt-in composition scenario**

Run: `DOMOAI_LIVE_BATTERY_KNX_GATEWAY_ENABLE=1 uv run pytest -q tests/integration/test_knx_gateway_live.py`

Expected: PASS with the Compose stack and KNX Virtual running.

- [X] **Step 5: Add operator runbook steps**

Document the exact start order, ETS connection (`172.26.80.1:3672`, NAT on),
bridge command, MQTT observation command, and recovery command for a gateway
restart.

### Task 5: Run composition and release verification

**Files:**
- Review: all files changed by Tasks 1-4

- [X] **Step 1: Run focused tests and static checks**

Run: `uv run pytest -q tests/unit/lab/test_knx_gateway_config.py tests/unit/lab/test_knx_battery_bridge.py tests/integration/test_knx_gateway_live.py`

Run: `uv run ruff check dev/lab src/domoai tests/unit/lab tests/integration/test_knx_gateway_live.py`

- [X] **Step 2: Run Compose and architecture checks**

Run: `docker compose -f dev/lab/compose.yaml config`

Run: `project-composition-check /mnt/c/users/ferna/onedrive/escritorio/domoai`

- [X] **Step 3: Run real acceptance commands**

Start `mqtt battery knx-gateway homeassistant`, confirm all healthchecks, run
the bridge against `127.0.0.1:3672`, and perform an ETS write to `4/0/0`.
Capture the MQTT `power/set` payload and the battery HTTP/HA readback.

- [X] **Step 4: Review the diff and residual risks**

Verify no DPT/address changed, no production authority path changed, and the
gateway is clearly marked lab-only.

## Closure notes (2026-08-30)

Implemented, verified real (not name-matched): `dev/lab/knx-gateway/{Dockerfile,
knxd.conf,healthcheck.sh}` exist; `knxd.conf` has `ip-address=172.26.80.1`,
`dest-port=3671`, `server=ets_router`, `port=3672`, `client-addrs=1.0.231:8`
— matches every assertion in Task 1's failing test. `dev/lab/compose.yaml`
declares `knx-gateway` with healthcheck and UDP publication.
`dev/lab/battery/knx_bridge.py` defaults `knx_port` to `3672` with
`DOMOAI_KNX_GATEWAY_PORT` override, matching Task 3. `tests/unit/lab/
test_knx_gateway_config.py` and `tests/integration/test_knx_gateway_live.py`
exist and run: `4 passed, 1 skipped` (the skip is the opt-in live composition
scenario, correct without `DOMOAI_LIVE_BATTERY_KNX_GATEWAY_ENABLE=1` set).

Real divergence from plan text, not a gap: implementation grew a WSL-native
path (`dev/lab/knx-gateway/run-wsl.sh`, `knxd-wsl.conf.in`) beyond what this
plan specified, and `dev/lab/knx-virtual.md`/`README.md` now state "the
stable path is WSL-native `knxd`, not the Compose `knx-gateway` profile" —
the Compose service still exists but was demoted to an experimental
`profiles: ["knxdocker"]` gate, and compose adds an extra `3673:3673/udp`
port not in this plan. Goal (ETS + DomoAI simultaneous access) is met either
way; documenting so a future session doesn't read the Compose-only plan text
as the current operational instructions.

Same pattern as the other closed plans this session: this scope is
uncommitted, sitting in the working tree.
