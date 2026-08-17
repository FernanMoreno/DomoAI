# Portable core skills

Portable `SKILL.md` procedures live under this directory after the runtime and
MCP contracts are stable. A skill may orchestrate semantic operations, but
runtime policy remains authoritative.

The `optimize-home-energy` procedure uses this v1 route table:

```text
discover_devices  → domotics.discover_devices
get_state         → domotics.get_state
optimize_scenario → ortools.optimize_scenario
validate_plan     → domotics.validate_plan
explain_solution  → ortools.explain_solution
operator_approval → operator.request_approval
execute_plan      → domotics.execute_plan
```

These are provider roles, not hard-coded MCP server names. Direct adapter,
vendor API, cluster path and arbitrary solver-code routes are outside the
portable core contract.
