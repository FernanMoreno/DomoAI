"""Command line boundary for offline administrative operations."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from domoai.admin.deployment_preflight import DeploymentPreflightRequest, run_preflight
from domoai.persistence.backup import BackupError, BackupService, BackupSource
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="domoai-admin")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup_commands = backup.add_subparsers(dest="backup_command", required=True)

    create = backup_commands.add_parser("create")
    create.add_argument("--database", type=Path, required=True)
    create.add_argument("--audit-database", type=Path, required=True)
    create.add_argument("--output-dir", type=Path, required=True)
    create.add_argument("--deployment-id", required=True)

    verify = backup_commands.add_parser("verify")
    verify.add_argument("--backup-dir", type=Path, required=True)

    restore = backup_commands.add_parser("restore")
    restore.add_argument("--backup-dir", type=Path, required=True)
    restore.add_argument("--target-data-dir", type=Path, required=True)
    restore.add_argument("--deployment-id", required=True)

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
    preflight.add_argument(
        "--caddyfile", type=Path, default=Path("deploy/reverse-proxy/Caddyfile")
    )
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
        manifest = await BackupService().create(
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
    result = await BackupService().restore(
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


def _run(args: argparse.Namespace) -> dict[str, object]:
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
    service = BackupService()
    if args.backup_command == "create":
        return asyncio.run(_create_backup(args))
    if args.backup_command == "verify":
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
