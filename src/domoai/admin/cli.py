"""Command line boundary for administrative backup operations."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from domoai.persistence.backup import BackupError, BackupService, BackupSource
from domoai.persistence.sqlite import SQLiteDatabase


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


def _run(args: argparse.Namespace) -> dict[str, object]:
    service = BackupService()
    if args.command != "backup":
        raise BackupError("backup_manifest_invalid")
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
        print(json.dumps(_run(args), sort_keys=True))
    except BackupError as error:
        print(json.dumps({"error": {"code": error.code}}, sort_keys=True))
        return 2
    return 0
