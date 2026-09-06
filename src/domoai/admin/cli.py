"""Command line boundary for offline administrative operations."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from domoai.admin.deployment_preflight import DeploymentPreflightRequest, run_preflight
from domoai.application.etcd_coordination import (
    build_external_etcd_http_client,
    build_external_lease_coordinator,
)
from domoai.application.multihost_qualification import (
    EtcdHttpQuorumProbe,
    MultiHostQualificationRunner,
    PsycopgPostgresHaProbe,
)
from domoai.config.settings import Settings
from domoai.domain.coordination import LeaseScope
from domoai.hil.multihost import JsonlFencingGatewayBridge
from domoai.mcp.token_lifecycle import TokenFileManager
from domoai.persistence.backup import (
    BackupError,
    BackupService,
    BackupSource,
    load_backup_encryption_key,
)
from domoai.persistence.postgres import PostgresDatabase
from domoai.persistence.postgres_migration import (
    PostgresMigrationError,
    migrate_sqlite_to_postgres,
)
from domoai.persistence.repositories import (
    RuntimeOwnershipRecoveryError,
    RuntimeOwnershipRepository,
)
from domoai.persistence.sqlite import SQLiteDatabase


def _preflight_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout must be a number") from error
    if not 0.1 <= timeout <= 10.0:
        raise argparse.ArgumentTypeError("timeout must be between 0.1 and 10 seconds")
    return timeout


def _positive_hours(value: str) -> float:
    try:
        hours = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("hours must be a number") from error
    if not 0 < hours <= 24 * 90:
        raise argparse.ArgumentTypeError("hours must be between 0 and 2160")
    return hours


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="domoai-admin")
    commands = parser.add_subparsers(dest="command", required=True)
    migrate = commands.add_parser(
        "migrate-postgres",
        help="copy an existing SQLite control plane into an empty PostgreSQL control plane",
    )
    migrate.add_argument("--source-database", type=Path, required=True)
    migrate.add_argument("--dsn", required=True)
    migrate.add_argument("--sslmode", choices=("require", "verify-full"), default="verify-full")
    migrate.add_argument("--sslrootcert", type=Path)
    migrate.add_argument("--sslcert", type=Path)
    migrate.add_argument("--sslkey", type=Path)
    qualify_multihost = commands.add_parser(
        "qualify-multihost",
        help="run attended etcd/PostgreSQL/final-hop fencing qualification from environment",
    )
    qualify_multihost.add_argument(
        "--confirm-physical-fencing",
        action="store_true",
        help="confirm that the gateway command may execute the documented safe physical probe",
    )
    qualify_multihost.add_argument(
        "--gateway-command",
        nargs="+",
        required=True,
        help="operator-owned JSONL bridge executable and arguments",
    )
    qualify_multihost.add_argument(
        "--safe-command",
        required=True,
        help="gateway-documented harmless command used only for this qualification",
    )
    qualify_multihost.add_argument("--output", type=Path, required=True)
    qualify_multihost.add_argument(
        "--evidence-valid-for-hours",
        type=_positive_hours,
        default=24.0,
    )
    backup = commands.add_parser("backup")
    backup_commands = backup.add_subparsers(dest="backup_command", required=True)

    create = backup_commands.add_parser("create")
    create.add_argument("--database", type=Path, required=True)
    create.add_argument("--audit-database", type=Path, required=True)
    create.add_argument("--output-dir", type=Path, required=True)
    create.add_argument("--deployment-id", required=True)
    create.add_argument("--encryption-key-file", type=Path)

    verify = backup_commands.add_parser("verify")
    verify.add_argument("--backup-dir", type=Path, required=True)
    verify.add_argument("--encryption-key-file", type=Path)

    restore = backup_commands.add_parser("restore")
    restore.add_argument("--backup-dir", type=Path, required=True)
    restore.add_argument("--target-data-dir", type=Path, required=True)
    restore.add_argument("--deployment-id", required=True)
    restore.add_argument("--encryption-key-file", type=Path)

    runtime = commands.add_parser("runtime")
    runtime_commands = runtime.add_subparsers(dest="runtime_command", required=True)
    release = runtime_commands.add_parser(
        "release-stale-owner",
        help="release a durable owner only when no live gateway holds its SQLite lock",
    )
    release.add_argument("--database", type=Path, required=True)
    release.add_argument("--deployment-id", required=True)
    release.add_argument("--owner-id", required=True)

    deployment = commands.add_parser("deployment")
    deployment_commands = deployment.add_subparsers(dest="deployment_command", required=True)
    preflight = deployment_commands.add_parser(
        "preflight",
        help="validate deployment artifacts without starting services or touching devices",
    )
    preflight.add_argument("--env-file", type=Path, default=Path("deploy/gateway.env"))
    preflight.add_argument("--clients-file", type=Path, default=Path("deploy/clients.json"))
    preflight.add_argument("--compose-file", type=Path, default=Path("deploy/compose.yaml"))
    preflight.add_argument("--caddyfile", type=Path, default=Path("deploy/reverse-proxy/Caddyfile"))
    preflight.add_argument(
        "--network",
        action="store_true",
        help="also perform bounded read-only TCP reachability checks",
    )
    preflight.add_argument(
        "--timeout-seconds",
        type=_preflight_timeout,
        default=2.0,
        help="per-dependency network timeout (0.1-10 seconds)",
    )

    tokens = commands.add_parser("tokens")
    token_commands = tokens.add_subparsers(dest="token_command", required=True)
    rotate = token_commands.add_parser("rotate")
    rotate.add_argument("--file", type=Path, required=True)
    rotate.add_argument("--client-id", required=True)
    rotate.add_argument("--scopes", default="read")
    rotate.add_argument("--tenant-id", default="default")
    rotate.add_argument("--households", default="default")
    rotate.add_argument("--roles", default="")
    rotate.add_argument("--areas", default="")
    rotate.add_argument("--devices", default="")
    rotate.add_argument("--capabilities", default="")
    rotate.add_argument("--operations", default="")
    rotate.add_argument("--ttl-days", type=float, default=30.0)
    revoke = token_commands.add_parser("revoke")
    revoke.add_argument("--file", type=Path, required=True)
    revoke.add_argument("--client-id", required=True)
    return parser


async def _create_backup(args: argparse.Namespace) -> dict[str, object]:
    for path in (args.database, args.audit_database):
        if path.is_symlink() or not path.is_file():
            raise BackupError("backup_source_unavailable")
    operational = SQLiteDatabase(args.database)
    audit = SQLiteDatabase(args.audit_database)
    try:
        try:
            await operational.open_existing()
            await audit.open_existing()
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
            raise BackupError("backup_source_unavailable") from error
        service = BackupService(
            encryption_key=(
                load_backup_encryption_key(args.encryption_key_file)
                if args.encryption_key_file is not None
                else None
            )
        )
        manifest = await service.create(
            sources=(BackupSource("operational", operational), BackupSource("audit", audit)),
            output_dir=args.output_dir,
            deployment_id=args.deployment_id,
        )
        return {
            "status": "created",
            "backup_id": manifest.backup_id,
            "members": sorted(member.name for member in manifest.members),
            "sizes": {member.name: member.size for member in manifest.members},
            "sha256": {member.name: member.sha256 for member in manifest.members},
        }
    finally:
        await operational.close()
        await audit.close()


async def _restore_backup(args: argparse.Namespace) -> dict[str, object]:
    service = BackupService(
        encryption_key=(
            load_backup_encryption_key(args.encryption_key_file)
            if args.encryption_key_file is not None
            else None
        )
    )
    result = await service.restore(
        backup_dir=args.backup_dir,
        target_data_dir=args.target_data_dir,
        deployment_id=args.deployment_id,
    )
    return result.to_dict()


async def _release_stale_owner(args: argparse.Namespace) -> dict[str, object]:
    database = SQLiteDatabase(args.database)
    advisory_lock = None
    try:
        try:
            await database.open_existing()
        except FileNotFoundError as error:
            raise BackupError("runtime_database_unavailable") from error
        advisory_lock = database.advisory_lock()
        try:
            await asyncio.to_thread(advisory_lock.acquire, blocking=False)
        except BlockingIOError as error:
            raise BackupError("runtime_owner_active") from error
        try:
            released = await RuntimeOwnershipRepository(database).release_stale(
                deployment_id=args.deployment_id,
                owner_id=args.owner_id,
            )
        except RuntimeOwnershipRecoveryError as error:
            raise BackupError(error.code) from error
        return {
            "deployment_id": args.deployment_id,
            "status": "released" if released else "already_released",
        }
    except BackupError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise BackupError("runtime_database_invalid") from error
    finally:
        if advisory_lock is not None:
            advisory_lock.release()
        await database.close()


async def _migrate_postgres(args: argparse.Namespace) -> dict[str, object]:
    source = SQLiteDatabase(args.source_database)
    destination: PostgresDatabase | None = None
    if any(path is not None for path in (args.sslrootcert, args.sslcert, args.sslkey)):
        if any(path is None for path in (args.sslrootcert, args.sslcert, args.sslkey)):
            raise BackupError("postgres_migration_mtls_incomplete")
        destination_dsn = PostgresDatabase.build_dsn(
            args.dsn,
            sslmode=args.sslmode,
            sslrootcert=args.sslrootcert,
            sslcert=args.sslcert,
            sslkey=args.sslkey,
        )
    else:
        destination_dsn = args.dsn
    destination = PostgresDatabase(destination_dsn)
    try:
        if source.path.is_symlink() or not source.path.is_file():
            raise BackupError("postgres_migration_source_unavailable")
        await source.open_existing()
        await destination.initialize()
        try:
            report = await migrate_sqlite_to_postgres(source, destination)
        except PostgresMigrationError as error:
            raise BackupError("postgres_migration_failed") from error
        return {
            "status": "migrated",
            "table_counts": report.table_counts,
            "payload_digests": report.payload_digests,
        }
    finally:
        await source.close()
        if destination is not None:
            await destination.close()


async def _qualify_multihost(args: argparse.Namespace) -> dict[str, object]:
    if not args.confirm_physical_fencing:
        raise BackupError("multihost_physical_probe_confirmation_required")
    settings = Settings.from_environment()
    if not settings.multi_host_enabled:
        raise BackupError("multihost_qualification_requires_multi_host")
    if len(settings.etcd_endpoints) not in {3, 5}:
        raise BackupError("multihost_qualification_requires_three_or_five_etcd_endpoints")
    if settings.postgres_dsn is None:
        raise BackupError("multihost_qualification_requires_postgres_dsn")
    postgres_certificates = (
        settings.postgres_sslrootcert,
        settings.postgres_sslcert,
        settings.postgres_sslkey,
    )
    if any(path is None for path in postgres_certificates):
        raise BackupError("multihost_qualification_requires_postgres_mtls")
    assert all(path is not None for path in postgres_certificates)
    certificate_paths = tuple(path for path in postgres_certificates if path is not None)
    if any(path.is_symlink() or not path.is_file() for path in certificate_paths):
        raise BackupError("multihost_qualification_requires_regular_postgres_certificates")
    gateway_identity = settings.multi_host_gateway_identity
    if gateway_identity is None:
        raise BackupError("multihost_qualification_requires_gateway_identity")

    etcd_client = build_external_etcd_http_client(settings)
    try:
        coordinator = build_external_lease_coordinator(settings)
    except (OSError, ValueError, RuntimeError) as error:
        await etcd_client.aclose()
        raise BackupError("multihost_qualification_coordinator_unavailable") from error
    try:
        postgres_dsn = PostgresDatabase.build_dsn(
            settings.postgres_dsn.get_secret_value(),
            sslmode=settings.postgres_sslmode,
            sslrootcert=certificate_paths[0],
            sslcert=certificate_paths[1],
            sslkey=certificate_paths[2],
        )
        runner = MultiHostQualificationRunner(
            coordinator=coordinator,
            quorum_probe=EtcdHttpQuorumProbe(
                settings.etcd_endpoints,
                client=etcd_client,
                timeout_seconds=settings.etcd_request_timeout_seconds,
            ),
            postgres_probe=PsycopgPostgresHaProbe(postgres_dsn),
            gateway=JsonlFencingGatewayBridge(args.gateway_command),
        )
        scope = LeaseScope(
            tenant_id=settings.mcp_tenant_id,
            household_id=settings.mcp_household_id,
            deployment_id=settings.mcp_deployment_id,
        )
        evidence = await runner.run(
            scope=scope,
            owner_id=f"qualification-{uuid4().hex}",
            gateway_identity=gateway_identity,
            safe_command=args.safe_command,
            ttl_seconds=settings.coordination_lease_seconds,
            evidence_ttl=timedelta(hours=args.evidence_valid_for_hours),
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.is_symlink():
            raise BackupError("multihost_qualification_output_must_not_be_symlink")
        args.output.write_text(evidence.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return {
            "status": evidence.status.value,
            "evidence_digest": evidence.evidence_digest,
            "expires_at": evidence.expires_at.isoformat(),
            "output": str(args.output),
        }
    finally:
        await etcd_client.aclose()
        close = getattr(coordinator, "aclose", None)
        if callable(close):
            await close()


def _token_command(args: argparse.Namespace) -> dict[str, object]:
    manager = TokenFileManager(args.file)
    if args.token_command == "rotate":
        if args.ttl_days <= 0:
            raise ValueError("token ttl must be positive")
        result = manager.rotate(
            args.client_id,
            scopes=[item.strip() for item in args.scopes.split(",") if item.strip()],
            tenant_id=args.tenant_id,
            household_ids=[item.strip() for item in args.households.split(",") if item.strip()],
            roles=[item.strip() for item in args.roles.split(",") if item.strip()] or None,
            area_ids=[item.strip() for item in args.areas.split(",") if item.strip()],
            device_ids=[item.strip() for item in args.devices.split(",") if item.strip()],
            capabilities=[item.strip() for item in args.capabilities.split(",") if item.strip()],
            operations=[item.strip() for item in args.operations.split(",") if item.strip()],
            ttl=timedelta(days=args.ttl_days),
        )
        # This is the one administrative secret channel where the new bearer
        # is intentionally returned. It is never persisted or sent through MCP.
        return {
            "status": "rotated",
            "client_id": result.client_id,
            "token": result.token,
            "created_at": result.created_at.isoformat(),
            "expires_at": result.expires_at.isoformat(),
        }
    if args.token_command == "revoke":
        return {
            "status": "revoked" if manager.revoke(args.client_id) else "unchanged",
            "client_id": args.client_id,
        }
    raise ValueError("unknown token command")


def _run(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "tokens":
        return _token_command(args)
    if args.command == "migrate-postgres":
        return asyncio.run(_migrate_postgres(args))
    if args.command == "qualify-multihost":
        return asyncio.run(_qualify_multihost(args))
    if args.command == "deployment" and args.deployment_command == "preflight":
        report = asyncio.run(
            run_preflight(
                DeploymentPreflightRequest(
                    env_file=args.env_file,
                    clients_file=args.clients_file,
                    compose_file=args.compose_file,
                    caddyfile=args.caddyfile,
                    network=args.network,
                    timeout_seconds=args.timeout_seconds,
                )
            )
        )
        return report.to_dict()
    if args.command == "runtime" and args.runtime_command == "release-stale-owner":
        return asyncio.run(_release_stale_owner(args))
    if args.command != "backup":
        raise BackupError("backup_manifest_invalid")
    if args.backup_command == "create":
        return asyncio.run(_create_backup(args))
    if args.backup_command == "verify":
        service = BackupService(
            encryption_key=(
                load_backup_encryption_key(args.encryption_key_file)
                if args.encryption_key_file is not None
                else None
            )
        )
        manifest = service.verify(args.backup_dir)
        return {
            "status": "verified",
            "backup_id": manifest.backup_id,
            "members": sorted(member.name for member in manifest.members),
        }
    if args.backup_command == "restore":
        return asyncio.run(_restore_backup(args))
    raise BackupError("backup_manifest_invalid")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = _run(args)
        print(json.dumps(result, sort_keys=True))
        return 2 if result.get("status") == "failed" else 0
    except BackupError as error:
        print(json.dumps({"error": {"code": error.code}}, sort_keys=True))
        return 2
