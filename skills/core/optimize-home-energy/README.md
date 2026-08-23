# optimize-home-energy skill

Portable Agent Skills procedure for coordinating:

```text
discover_devices → get_state → get_energy_context → optimize_scenario
→ validate_plan → explain_solution → operator_approval
→ commit_or_schedule_bundle
```

The fixture contract is validated by
`tests/integration/test_core_skill.py`. The procedure returns a proposal from
the optimizer and hands all mutations to the DomoAI runtime safety boundary.

Its machine-readable bindings are validated by
`src/domoai/skills/validator.py`. The reference host maps the single `mcp` role
to semantic calls, pauses at `operator.request_approval` when policy requires
it, and resumes only through `mcp.commit_or_schedule_bundle` with the complete
ordered bundle. For bundles, the host returns the `bundle_digest` displayed in
the approval explanation; it is distinct from the member digests used by the
MCP runtime. The runtime owns the durable saga: future-only members are
scheduled together, while physical members are reported explicitly as
completed, failed, partially committed or unknown. No automatic rollback is
claimed for a physical write that already happened.

Host extensions may adapt presentation or invocation details, but all semantic
operations remain on the one general MCP connection. They must preserve the
declared order and may not replace `validate_plan`, `operator_approval` or
`commit_or_schedule_bundle` with direct adapter calls.
