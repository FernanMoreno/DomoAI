# Architecture contracts

The source-derived gate is:

```bash
uv run python scripts/check_architecture_contracts.py
```

It enforces the two boundaries that must never drift as the project grows:

- `domoai.domain` is independent of all other DomoAI packages.
- Protocol adapters cannot import one another.

`.importlinter` additionally records the intended acyclic sibling and layer
direction. Its current output still reports historical orchestration edges
(`runtime` contains legacy orchestration modules that reach application,
adapters and persistence). Those are explicit migration risks, not silently
redefined as valid architecture. New code must use ports and the composition
root rather than adding to those edges.

Every architecture change requires a composition scenario covering the first
consumer/provider boundary affected by the import.
