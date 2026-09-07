# Portable core skills

Portable `SKILL.md` procedures live under this directory after the runtime and
MCP contracts are stable. The deterministic catalog is validated with
`domoai.skills.catalog.load_core_catalog()`. A Skill may orchestrate semantic
operations, but runtime policy remains authoritative.

The Phase 2 catalog contains:

- `optimize-home-energy`: proposal and approved bundle workflow.
- `optimize-ev-charging`: deadline- and power-bounded EV charging.
- `thermal-comfort`: HVAC comfort and anti-cycle proposals.
- `solar-self-consumption`: forecast-aware flexible-load shifting.
- `battery-arbitrage`: qualification- and feedback-gated battery proposals.
- `night-mode`: bounded lighting/climate scenes with security isolation.
- `vacation-mode`: expiring protection and savings proposals.
- `device-diagnostics`: read-only inventory/state diagnostics.
- `commission-new-device`: read-only discovery and commissioning evidence.

Contract v4 requires explicit context, allowed tools/resources, forbidden
tools, freshness, approval scope and failure behavior. Direct adapters, vendor
APIs and solver calls are forbidden.

The `optimize-home-energy` procedure uses one general MCP route table:

```text
discover_devices         → mcp.discover_devices
get_state                → mcp.get_state
get_energy_context      → mcp.get_energy_context
optimize_scenario       → mcp.optimize_scenario
validate_plan           → mcp.validate_plan
explain_solution        → mcp.explain_solution
operator_approval       → operator.request_approval
commit_or_schedule_bundle → mcp.commit_or_schedule_bundle
```

`mcp` is one general connection role, not a hard-coded server brand. Direct
adapter, vendor API, cluster path, second-server and arbitrary solver-code
routes are outside the portable core contract.
