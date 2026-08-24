# Architecture contracts

The source-derived gate is:

```bash
uv run python scripts/check_architecture_contracts.py
```

It enforces the two boundaries that must never drift as the project grows:

- `domoai.domain` is independent of all other DomoAI packages.
- Protocol adapters cannot import one another.

`.importlinter` is the authoritative acyclic sibling and layer-direction gate.
The runtime package is limited to the low-level kernel; application owns
orchestration, and lab owns replay/twin fixtures. New code must use ports and
the composition root rather than adding reverse package dependencies.

The complete policy is blocking in local composition checks and CI:

```bash
uv run lint-imports
```

The source-derived checker remains complementary: it enforces domain purity
and protocol-adapter independence with source-level diagnostics.

Every architecture change requires a composition scenario covering the first
consumer/provider boundary affected by the import.
