# Skills

## Portable core

Procedures under `skills/core/` use the Agent Skills `SKILL.md` convention and
must remain host-agnostic. They may coordinate semantic reads, optimization,
explanation, validation and explicit operator approval.

The portable procedure declares semantic operation bindings by provider role:
`domotics`, `ortools` and `operator`. Host configuration maps those roles to
concrete MCP connections; the core skill does not name server instances,
vendors, protocols, adapters or solver code.

## Host extensions

Host-specific additions belong under `skills/hosts/<host>/` and should only
adapt invocation or presentation details. They must not duplicate policy rules,
authorize sensitive actions, call vendor adapters, or bypass the runtime plan
boundary. The portable core procedure remains the behavioral source of truth.

The DomoAI validator rejects missing, duplicate or unknown bindings and keeps
the approval-before-execution order. A host may change invocation or
presentation details only; it may not replace the runtime plan boundary.
