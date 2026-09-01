# MCP to KNX Virtual Battery End-to-End Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove and close the supervised MCP → scheduler → KNX Virtual → MQTT battery loop while keeping production battery authority disabled.

**Architecture:** Reuse the existing MCP, PlanRepository, ApprovalStore, Scheduler, ExecutionAdmission and PlanExecutor boundaries. Add only the provider-specific KNX lab takeover evidence needed by `BatteryControlCoordinator`; no MCP or bridge direct-write path is introduced. The live scenario uses the existing native WSL `knxd` endpoint and disposable Docker battery/MQTT services.

**Tech Stack:** Python 3.12, Pydantic v2, pytest/pytest-asyncio, FastMCP, SQLite, xknx, MQTT, uv, Ruff, mypy and Import Linter.

**Spec:** `specs/170-mcp-knx-battery-e2e/spec.md`

## Global Constraints

- No Docker KNX gateway is added or revived.
- No physical hardware or HIL qualification is claimed.
- A writable energy command requires an explicit server-owned binding.
- Every non-zero test command ends with a confirmed stop in cleanup.
- Live tests are opt-in and use real disposable infrastructure.
- No credentials or tokens enter tests, plans, logs or vault notes.

---

### Task 1: Add the failing KNX takeover contract test

**Files:**
- Modify: `tests/contract/test_knx_adapter.py`
- Test: `tests/contract/test_knx_adapter.py`

**Interfaces:**
- Consumes: `KnxAdapter`, `InMemoryKnxTransport`, `ControlTakeoverRequest`, `ControlLeaseStatus`.
- Produces: a regression test requiring `await adapter.acquire_control(request)` to return a matching acquired `TakeoverResult` whose baseline uses the `KnxGroupValue.observed_at` timestamp.

- [x] **Step 1: Write the failing test**

```python
async def test_knx_battery_takeover_returns_observed_feedback_baseline():
    observed_at = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    transport = InMemoryKnxTransport(
        [KnxGroupValue("4/0/2", "9.024", 0.0, observed_at)]
    )
    adapter = KnxAdapter(transport, load_mapping(Path("dev/lab/configs/knx-battery-virtual.json")))
    await adapter.connect()
    await adapter.discover()
    result = await adapter.acquire_control(
        ControlTakeoverRequest(
            owner="domoai-lab",
            device_id="lab.virtual-battery",
            plan_id="plan-1",
            first_command_id="command-1",
            first_command="charge_battery",
            first_command_value=1.0,
            native_scheduler_status="inactive",
            allow_native_takeover=False,
            lease_seconds=60.0,
        )
    )
    assert result.status is ControlLeaseStatus.ACQUIRED
    assert result.baseline is not None
    assert result.baseline.observed_at == observed_at
```

Use the canonical id actually returned by `canonical_device_id()` in the test; the expected first run must fail because `KnxAdapter` has no takeover method.

- [x] **Step 2: Run test to verify it fails**

Run: `uv run pytest -q tests/contract/test_knx_adapter.py -k takeover_returns_observed_feedback_baseline`

Expected: FAIL with an `AttributeError` showing that the adapter does not expose `acquire_control`.

### Task 2: Implement the minimal KNX lab takeover evidence

**Files:**
- Modify: `src/domoai/adapters/knx/adapter.py`
- Test: `tests/contract/test_knx_adapter.py`

**Interfaces:**
- Consumes: the configured `battery.power` mapping and live/in-memory transport `read_group` contract.
- Produces: `async KnxAdapter.acquire_control(request: ControlTakeoverRequest) -> TakeoverResult`.

- [x] **Step 1: Implement the smallest passing behavior**

Read the exact configured feedback group, reject missing/unavailable/non-finite
baseline or an active native scheduler, and construct a digest-bound
`TakeoverResult` with `PhysicalBaseline.source_ref.adapter_id == "knx"` and
`observed_at` copied from the transport response.

- [x] **Step 2: Run the contract test**

Run: `uv run pytest -q tests/contract/test_knx_adapter.py -k takeover`

Expected: PASS, with failures represented as a rejected `TakeoverResult` rather than an exception for expected unavailable feedback.

### Task 3: Add the failing composed MCP/scheduler test

**Files:**
- Create: `tests/composition/test_mcp_knx_battery_e2e.py`
- Modify: `tests/fixtures/knx_battery.py` if a shared fixture is necessary

**Interfaces:**
- Consumes: `create_domotics_server`, `DomoticsMcpContext`, `build_runtime`-equivalent composition, `InMemoryKnxTransport`, disposable `SQLiteDatabase`.
- Produces: one test that invokes `discover_devices`, `validate_plan`, `request_approval`, `schedule_plan`, then `Scheduler.run_due()` and verifies outcome/readback.

- [x] **Step 1: Write the failing composed test**

The test must create an explicit `DispatchableBatteryBinding` with provider
`knx`, configure a confirmation policy, call MCP tools through
`ClientSession`, schedule a future plan with an exact `ExecutionWindow`, run
the due scheduler, and assert the transport captured `4/0/0` plus a confirmed
`battery.power` readback. It must also assert the persisted plan is terminal.

- [x] **Step 2: Run the composed test to verify the correct failure**

Run: `uv run pytest -q tests/composition/test_mcp_knx_battery_e2e.py -vv`

Expected: FAIL first at the missing KNX takeover contract or at an explicitly identified composition boundary, not because of test setup or malformed plan data.

### Task 4: Complete deterministic regression coverage

**Files:**
- Modify: `tests/composition/test_mcp_knx_battery_e2e.py`
- Modify: `tests/contract/test_domotics_mcp_contract.py` only if a shared MCP contract is missing

**Interfaces:**
- Consumes: the passing composed flow from Task 3.
- Produces: tests for no approval, duplicate delivery/idempotency, and cleanup stop.

- [x] **Step 1: Add no-approval rejection test**

Assert that `schedule_plan`/`execute_plan` returns an error envelope and the
KNX transport has no accepted battery write.

- [x] **Step 2: Add duplicate execution test**

Deliver the same scheduled plan twice and assert SQLite execution claim plus
adapter idempotency prevent a second accepted command.

- [x] **Step 3: Add cleanup-stop test**

Run a non-zero command in a `try/finally`, issue `stop_battery` on the same
adapter boundary and assert a final zero readback.

- [x] **Step 4: Run the deterministic regression set**

Run: `uv run pytest -q tests/composition/test_mcp_knx_battery_e2e.py tests/contract/test_domotics_mcp_contract.py`

Expected: PASS.

### Task 5: Add the opt-in live MCP flow

**Files:**
- Create: `tests/integration/test_live_mcp_knx_battery_e2e.py`
- Modify: `specs/170-mcp-knx-battery-e2e/quickstart.md`

**Interfaces:**
- Consumes: active native WSL KNX gateway, bridge, Docker battery/MQTT, MCP tools and runtime settings.
- Produces: opt-in live test with a disposable database and guaranteed final stop.

- [x] **Step 1: Write the live test**

Guard on `DOMOAI_LIVE_MCP_KNX_BATTERY_ENABLE=1`. Load the existing lab
environment without printing secrets, resolve the canonical device from live
discovery, and exercise the complete MCP flow. Verify Docker battery HTTP
state, KNX adapter readback groups, one durable outcome and final zero power.

- [x] **Step 2: Run the live test**

Run: `set -a; source dev/lab/.env; set +a; DOMOAI_LIVE_MCP_KNX_BATTERY_ENABLE=1 uv run pytest -q tests/integration/test_live_mcp_knx_battery_e2e.py -vv`

Expected: PASS against the active lab or a clear infrastructure skip; never a false production qualification.

### Task 6: Verification and review

**Files:**
- Modify: `specs/170-mcp-knx-battery-e2e/tasks.md`
- Modify: `docs/superpowers/plans/2026-09-01-mcp-knx-battery-e2e.md`

- [x] **Step 1: Run focused checks**

Run: `uv run pytest -q tests/contract/test_knx_adapter.py tests/composition/test_mcp_knx_battery_e2e.py tests/integration/test_live_mcp_knx_battery_e2e.py`

- [x] **Step 2: Run architecture and static checks**

Run: `uv run ruff check src tests` and `uv run mypy src` and `lint-imports`.

- [x] **Step 3: Run composition review tooling**

The named `project-composition-check` and `project-composition-review` commands
are not installed in this environment. The repository's
`bash scripts/composition_check.sh` was run in fast and full modes, and the
system-composition review was performed manually across MCP, persistence,
scheduler, executor, KNX transport, bridge and Docker battery boundaries.

Run: `project-composition-check .` and `project-composition-review . codex`.

- [x] **Step 4: Refresh Graphify and review the diff**

Run the project Graphify refresh command, inspect `git diff --check` and the
focused diff, then mark only verified tasks as `[x]`. The refreshed graph was
queried to verify the MCP → scheduler → KNX → readback → durable outcome path.

## Review outcome

`PASS WITH RISKS`: the supervised virtual-lab path is proven end to end. The
KNX adapter takeover is explicitly lab-only because KNX/IP has no portable
ownership primitive. Physical battery authority remains blocked on real
takeover, lease/watchdog, crash-safe stop, HIL and deployment qualification.
