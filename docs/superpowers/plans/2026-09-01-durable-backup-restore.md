# Durable Runtime Backup and Restore

## Goal

Provide an administrator-only, local recovery path for operational and audit SQLite data without copying live WAL files or exposing restore authority to an agent.

## Implementation sequence

1. Add contract and red tests for online backup, manifest integrity, safe paths, and stable redacted errors.
2. Add `SQLiteDatabase.backup_to()` and migration-ledger introspection, owned by the existing serialized storage lane.
3. Implement `BackupManifest`, `BackupMember`, and `BackupService` with temporary publication, SHA-256, SQLite integrity checks, completion marker, and atomic rename.
4. Connect online creation to `RuntimeComposition` through both existing storage workers.
5. Implement verify and staged restore. Refuse active/uncertain `runtime_ownership`, validate before replacement, keep rollback copies, and never execute plans.
6. Add `domoai-admin backup create|verify|restore`; keep it outside MCP and redact all output/errors.
7. Add restart/recovery integration tests and Docker/WSL operator documentation.
8. Run focused tests, repository quality gates, composition review, and vault transaction.

## Design constraints

- The operational and audit members are independently consistent; the manifest must not claim cross-database transaction atomicity.
- Restore is offline and administrative. No lab, KNX, Home Assistant, hardware, or HIL configuration changes are part of this work.
- No remote storage, encryption, signatures, Kubernetes, or physical qualification claims are added by this increment.
- Runtime ownership and restore share a sidecar process lock. Runtime holds it for its lifetime; restore acquires it non-blocking and retains it through the target check and replacement.
- Restore verifies copied staging members before local migration, so source mutation during copy cannot install unverified bytes.

## Verification checkpoints

- Red tests exist before each production behavior.
- Corrupt member/manifest is rejected without target mutation.
- Active or uncertain owner is rejected before target mutation.
- A restored runtime reopens normally and does not replay interrupted execution.
- `git diff --check`, focused tests, Ruff, mypy, Import Linter, and composition checks pass.
