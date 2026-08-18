# Portable core skills

Portable `SKILL.md` procedures live under this directory after the runtime and
MCP contracts are stable. A skill may orchestrate semantic operations, but
runtime policy remains authoritative.

The `optimize-home-energy` procedure uses one general MCP route table:

```text
discover_devices  → mcp.discover_devices
get_state         → mcp.get_state
optimize_scenario → mcp.optimize_scenario
validate_plan     → mcp.validate_plan
explain_solution  → mcp.explain_solution
operator_approval → operator.request_approval
execute_plan      → mcp.execute_plan
```

`mcp` is one general connection role, not a hard-coded server brand. Direct
adapter, vendor API, cluster path, second-server and arbitrary solver-code
routes are outside the portable core contract.
