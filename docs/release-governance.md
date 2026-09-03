# Release governance

The CI workflow has one authoritative aggregate check, `Required release
gates`. It depends on every release-critical job and fails when any dependency
is `failure`, `cancelled` or `skipped`. The individual checks remain visible
for diagnosis.

`main` is configured with strict required checks. The protected contexts are:

```text
Lock file consistency
Lint (ruff)
Typecheck (mypy)
Test (unit)
Test (contract)
Test (integration)
Test (performance)
Test (composition)
Package build and entrypoints
Schema freshness
Runtime contract documentation
Source architecture invariants
Import Linter (blocking)
Dependency vulnerability audit
Required release gates
```

The evidence job publishes `ci-evidence-${{ github.sha }}` with the job
results and downloaded JUnit/coverage artifacts. It is intentionally a report;
the aggregate gate is what blocks a merge.

To reapply the repository setting after an administrative change, use the
GitHub branch-protection API with the exact contexts above and `strict=true`.
Never store bearer tokens or other deployment secrets in this file.
