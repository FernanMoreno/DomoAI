# Plan: authenticated remote operational metrics

## Outcome

Expose the existing bounded runtime snapshot through an opt-in,
bearer-authenticated Prometheus text endpoint and make it reachable through the
deployment proxy without adding a second runtime or write path.

## Constraints

- Keep the existing single-writer runtime and hash-only bearer model.
- Do not export secrets, client identity or arbitrary device metadata.
- Fail closed on missing credentials, render failures and oversized output.
- Leave distributed aggregation, OTLP push and active-active fencing as later
  deployment work.

## Execution order

1. Add Spec Kit artifacts and red tests.
2. Implement renderer, settings and gateway route.
3. Wire Caddy and deployment documentation.
4. Run focused and full verification, then update audit evidence.
