# Universal MCP Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** Make the single client-neutral MCP boundary and the actual adapter
coverage explicit, testable and safe to consume before adding new integrations.

**Architecture:** The configured gateway continues to build one `FastMCP`
server through `create_unified_server`. This phase adds only a sanitized,
documentation-level coverage inventory and regression checks around shared
context identity; it does not create a second endpoint, change adapter routing,
or grant any new execution authority.

**Tech Stack:** Python 3.12, FastMCP, Pydantic, pytest, Markdown,
Import Linter.

**Spec:** [Universal MCP Baseline and Coverage Inventory](../../../specs/196-universal-mcp-baseline/spec.md)

## Global Constraints

- Keep one public standard MCP endpoint and one shared runtime.
- Preserve `preview → prepare → approval/admission → execute → readback →
  audit` as the only physical mutation path.
- Do not add vendor-specific MCP tools or public adapters in this phase.
  Protocol connectors remain internal to the universal adapter.
- Do not put credential-shaped data in documentation, resources or tests.
- Do not claim fixture/lab coverage is physical qualification.

---

### Task 1: Lock the single-runtime MCP contract

**Files:**

- Modify: `tests/contract/test_unified_mcp_contract.py`
- Modify: `tests/unit/mcp/test_gateway.py`
- Read: `src/domoai/mcp/unified_server.py`
- Read: `src/domoai/mcp/configured.py`

**Interfaces:**

- Consumes: `UnifiedMcpContext(domotics, optimizer)` and
  `create_unified_server(context, settings=None, runtime=None)`.
- Produces: regression evidence that `domotics.registry is optimizer.registry`
  and `domotics.facade.plan_service is optimizer.plan_service` at the only
  configured public MCP builder.

- [x] **Step 1: Add the failing unified-context identity test**

  Add a test beside `test_unified_server_exposes_one_complete_semantic_catalog`
  that builds the existing fixture context, then asserts the two identities:

  ```python
  @pytest.mark.asyncio
  async def test_unified_context_has_one_registry_and_plan_boundary() -> None:
      _, context = await build_context()
      assert context.domotics.registry is context.optimizer.registry
      assert context.domotics.facade.plan_service is context.optimizer.plan_service
  ```

- [x] **Step 2: Run the focused test before implementation**

  Run: `uv run pytest -q tests/contract/test_unified_mcp_contract.py`

  Expected: the test is initially absent; once added, it documents the already
  intended invariant. If it fails, stop and trace the builder boundary before
  changing production code.

- [x] **Step 3: Add gateway-level configured-builder regression coverage**

  In `tests/unit/mcp/test_gateway.py`, add a test that calls
  `create_gateway_server(context, settings)` and verifies one configured
  Streamable HTTP path is set. Reuse `_build_context()` and assert the existing
  `UnifiedMcpContext` identities instead of creating another runtime:

  ```python
  assert server.settings.streamable_http_path == settings.mcp_path
  assert context.domotics.registry is context.optimizer.registry
  assert context.domotics.facade.plan_service is context.optimizer.plan_service
  ```

- [x] **Step 4: Run focused MCP/gateway contracts**

  Run:

  ```bash
  uv run pytest -q tests/contract/test_unified_mcp_contract.py tests/unit/mcp/test_gateway.py
  ```

  Expected: all selected tests pass; no server construction opens a second
  adapter or executor.

### Task 2: Publish a truthful adapter coverage inventory

**Files:**

- Create: `docs/adapter-coverage.md`
- Modify: `docs/unified-mcp.md`
- Modify: `docs/adapter-sdk.md`
- Modify: `docs/contracts.md`
- Test: `tests/contract/test_gateway_deployment_assets.py`

**Interfaces:**

- Consumes: existing concrete adapters (`home_assistant`, `matter`,
  `zigbee2mqtt`, `knx`, `modbus`), `fixtures`, Provider SDK and the
  qualification terminology already used by preflight reports.
- Produces: a static, sanitized coverage matrix whose categories are `native`,
  `via_home_assistant`, `plugin`, `fixture_or_simulation`, `unavailable`, and
  `physically_qualified`.

- [x] **Step 1: Write the documentation-contract test first**

  Add a test that reads `docs/adapter-coverage.md` and asserts it includes all
  six category names, every currently shipped adapter name, and none of the
  forbidden credential fragments:

  ```python
  forbidden = {"token", "password", "secret", "private_key", "dsn"}
  assert {"native", "via_home_assistant", "plugin"} <= categories
  assert not any(fragment in coverage.casefold() for fragment in forbidden)
  ```

  Keep the check narrow: it must not reject normal security prose elsewhere in
  repository documentation.

- [x] **Step 2: Run the documentation test and observe the missing file**

  Run: `uv run pytest -q tests/contract/test_gateway_deployment_assets.py`

  Expected: fail because `docs/adapter-coverage.md` does not exist.

- [x] **Step 3: Write `docs/adapter-coverage.md`**

  Include one table per adapter with: adapter ID, protocol, coverage category,
  discovery/read/write/readback status, the evidence class, and explicit
  limits. State that Home Assistant is indirect coverage and that fixture,
  lab, or blocked external evidence never grants physical qualification.

- [x] **Step 4: Align the public documentation**

  Update:

  - `docs/unified-mcp.md`: one endpoint, no direct execution bypass, and a
    link to the coverage matrix.
  - `docs/adapter-sdk.md`: adapters and plugins map into the same matrix;
    plugin does not mean installed or qualified.
  - `docs/contracts.md`: document the safe lifecycle and distinguish resource
    visibility from authority.

- [x] **Step 5: Run documentation and contract verification**

  Run:

  ```bash
  uv run pytest -q tests/contract/test_gateway_deployment_assets.py
  uv run python scripts/check_runtime_contract_docs.py
  ```

  Expected: the inventory is complete for current shipped adapters, has no
  credential-shaped values, and runtime documentation remains coherent.

### Task 3: Replace the obsolete pending-work index

**Files:**

- Create: `docs/program-status-2026-09-06.md`
- Modify: `README.md`
- Test: `tests/contract/test_gateway_deployment_assets.py`

**Interfaces:**

- Consumes: current audit documents, `specs/133`, `specs/140`, `specs/141`,
  the program design and the coverage inventory.
- Produces: a current status index that separates closed software work, planned
  universal-MCP phases and external evidence gates.

- [x] **Step 1: Add a failing freshness assertion**

  Extend the documentation contract test with an assertion that
  `docs/program-status-2026-09-06.md` links both the 2026-09-06 comparison and the three
  externally blocked specs. It must reject stale claims such as
  `specs/001–specs/019` being the whole project scope.

- [x] **Step 2: Run the focused test**

  Run: `uv run pytest -q tests/contract/test_gateway_deployment_assets.py`

  Expected: fail until the legacy v1 index is replaced.

- [x] **Step 3: Publish the index as a versioned status document, not a backlog copy**

  Preserve historical evidence links, then add:

  - completed foundation through Specs 195;
  - the universal-MCP program phases and their status as planned;
  - external gates for HIL, real dependencies and independent provider
    contract; and
  - a rule that hardware/active-active status cannot be inferred from a local
    suite.

  Add a short link from `README.md` to the updated status/coverage documents.

- [x] **Step 4: Run final phase verification**

  Run:

  ```bash
  uv run pytest -q tests/contract/test_unified_mcp_contract.py tests/unit/mcp/test_gateway.py tests/contract/test_gateway_deployment_assets.py
  uv run ruff check .
  uv run mypy src
  uv run python scripts/check_architecture_contracts.py
  uv run python scripts/check_runtime_contract_docs.py
  project-composition-check "$(cat .ai/project-name)"
  git diff --check
  ```

  Expected: every command exits 0. If a check fails, identify the first broken
  boundary; do not relax an authority, redaction or qualification assertion to
  make it pass.

## Phase 0 completion review

- [x] Confirm no production MCP builder other than the unified gateway can
  acquire its own adapter/runtime/executor.
- [x] Confirm the coverage document distinguishes native connector, indirect, internal extension,
  fixture and qualification evidence.
- [x] Confirm no new physical mutation tool was added.
- [x] Run `system-composition-review` for MCP/configuration/docs/test boundary.
- [x] Review the final diff before beginning Phase 1.
