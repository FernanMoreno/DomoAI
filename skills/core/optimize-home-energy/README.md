# optimize-home-energy skill

Portable Agent Skills procedure for coordinating:

```text
discover_devices → get_state → optimize_scenario → validate_plan
→ explain_solution → operator_approval → execute_plan
```

The fixture contract is validated by
`tests/integration/test_core_skill.py`. The procedure returns a proposal from
the optimizer and hands all mutations to the DomoAI runtime safety boundary.

Its machine-readable bindings are validated by
`src/domoai/skills/validator.py`. The reference host maps the single `mcp` role
to semantic calls, pauses at `operator.request_approval` when policy requires
it, and resumes only through `mcp.execute_plan` with the matching digest.

Host extensions may adapt presentation or invocation details, but all semantic
operations remain on the one general MCP connection. They must preserve the
declared order and may not replace `validate_plan`, `operator_approval` or
`execute_plan` with direct adapter calls.
