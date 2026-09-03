"""Validated administrative backup and restore for runtime SQLite data."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from domoai.persistence.serialized import SerializedStorageExecutor
from domoai.persistence.sqlite import (
    MIGRATIONS_DIR,
    SQLiteAdvisoryLock,
    SQLiteBackupResult,
    SQLiteDatabase,
)
from domoai.runtime.clock import Clock, SystemClock

BACKUP_FORMAT_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
COMPLETION_FILENAME = "COMPLETE"
MEMBER_FILENAMES = {
    "operational": "domoai.sqlite3",
    "audit": "domoai-audit.sqlite3",
}
REQUIRED_MEMBERS = frozenset(MEMBER_FILENAMES)


class BackupError(RuntimeError):
    """Sanitized, stable administrative backup/restore failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class BackupSource:
    """One database and the serialized worker that owns it, if any."""

    name: str
    database: SQLiteDatabase
    storage: SerializedStorageExecutor | None = None


@dataclass(frozen=True)
class BackupMember:
    name: str
    filename: str
    source_label: str
    started_at: str
    completed_at: str
    size: int
    sha256: str
    integrity_check: str
    schema_migrations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "filename": self.filename,
            "source_label": self.source_label,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "size": self.size,
            "sha256": self.sha256,
            "integrity_check": self.integrity_check,
            "schema_migrations": list(self.schema_migrations),
        }

    @classmethod
    def from_dict(cls, payload: object) -> BackupMember:
        if not isinstance(payload, dict):
            raise BackupError("backup_manifest_invalid")
        required = {
            "name",
            "filename",
            "source_label",
            "started_at",
            "completed_at",
            "size",
            "sha256",
            "integrity_check",
            "schema_migrations",
        }
        if set(payload) != required:
            raise BackupError("backup_manifest_invalid")
        name = payload["name"]
        filename = payload["filename"]
        source_label = payload["source_label"]
        started_at = payload["started_at"]
        completed_at = payload["completed_at"]
        size = payload["size"]
        sha256 = payload["sha256"]
        integrity_check = payload["integrity_check"]
        migrations = payload["schema_migrations"]
        if (
            not isinstance(name, str)
            or not isinstance(filename, str)
            or not isinstance(source_label, str)
            or not isinstance(started_at, str)
            or not isinstance(completed_at, str)
            or not _valid_timestamp(started_at)
            or not _valid_timestamp(completed_at)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
            or integrity_check != "ok"
            or not isinstance(migrations, list)
            or not all(isinstance(item, str) for item in migrations)
            or Path(filename).name != filename
            or filename in {"", ".", ".."}
        ):
            raise BackupError("backup_manifest_invalid")
        return cls(
            name=name,
            filename=filename,
            source_label=source_label,
            started_at=started_at,
            completed_at=completed_at,
            size=size,
            sha256=sha256,
            integrity_check=integrity_check,
            schema_migrations=tuple(migrations),
        )


@dataclass(frozen=True)
class BackupManifest:
    backup_id: str
    format_version: int
    deployment_id: str
    created_at: str
    completed_at: str
    members: tuple[BackupMember, ...]
    manifest_sha256: str
    status: str

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "backup_id": self.backup_id,
            "format_version": self.format_version,
            "deployment_id": self.deployment_id,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "members": [member.to_dict() for member in self.members],
            "status": self.status,
        }
        if include_digest:
            payload["manifest_sha256"] = self.manifest_sha256
        return payload

    @classmethod
    def from_dict(cls, payload: object) -> BackupManifest:
        if not isinstance(payload, dict):
            raise BackupError("backup_manifest_invalid")
        required = {
            "backup_id",
            "format_version",
            "deployment_id",
            "created_at",
            "completed_at",
            "members",
            "status",
            "manifest_sha256",
        }
        if set(payload) != required:
            raise BackupError("backup_manifest_invalid")
        backup_id = payload["backup_id"]
        format_version = payload["format_version"]
        deployment_id = payload["deployment_id"]
        created_at = payload["created_at"]
        completed_at = payload["completed_at"]
        members_payload = payload["members"]
        status = payload["status"]
        manifest_sha256 = payload["manifest_sha256"]
        if (
            not isinstance(backup_id, str)
            or not backup_id
            or any(
                character not in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ._-"
                for character in backup_id
            )
            or not isinstance(format_version, int)
            or isinstance(format_version, bool)
            or not isinstance(deployment_id, str)
            or not deployment_id
            or "\n" in deployment_id
            or not isinstance(created_at, str)
            or not isinstance(completed_at, str)
            or not _valid_timestamp(created_at)
            or not _valid_timestamp(completed_at)
            or not isinstance(members_payload, list)
            or not isinstance(status, str)
            or not isinstance(manifest_sha256, str)
            or len(manifest_sha256) != 64
        ):
            raise BackupError("backup_manifest_invalid")
        members = tuple(BackupMember.from_dict(item) for item in members_payload)
        if len({member.name for member in members}) != len(members):
            raise BackupError("backup_manifest_invalid")
        return cls(
            backup_id=backup_id,
            format_version=format_version,
            deployment_id=deployment_id,
            created_at=created_at,
            completed_at=completed_at,
            members=members,
            manifest_sha256=manifest_sha256,
            status=status,
        )


@dataclass(frozen=True)
class RestoreResult:
    backup_id: str
    deployment_id: str
    rollback_directory: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "restored",
            "backup_id": self.backup_id,
            "deployment_id": self.deployment_id,
            "rollback_directory": self.rollback_directory,
        }


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _manifest_digest(manifest: BackupManifest) -> str:
    return hashlib.sha256(_canonical_json(manifest.to_dict(include_digest=False))).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _valid_timestamp(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


async def _to_thread_drain[T](function: Callable[..., T], *args: object) -> T:
    """Wait for a blocking operation to finish before propagating cancel."""

    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(task)
        except BaseException:
            pass
        raise


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_directory(path: Path, *, error_code: str) -> Path:
    try:
        if path.exists() and (path.is_symlink() or not path.is_dir()):
            raise BackupError(error_code)
        path.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise BackupError(error_code)
        return path.resolve()
    except BackupError:
        raise
    except (OSError, RuntimeError) as error:
        raise BackupError(error_code) from error


def _safe_file(path: Path, *, error_code: str) -> None:
    try:
        safe = not path.is_symlink() and path.is_file()
    except OSError as error:
        raise BackupError(error_code) from error
    if not safe:
        raise BackupError(error_code)


def _read_sqlite_metadata(path: Path) -> tuple[str, tuple[str, ...]]:
    connection: sqlite3.Connection | None = None
    try:
        _safe_file(path, error_code="backup_member_invalid")
        uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        row = connection.execute("PRAGMA integrity_check").fetchone()
        integrity = str(row[0]) if row else ""
        if integrity != "ok":
            raise BackupError("backup_integrity_failed")
        migrations = tuple(
            str(item[0])
            for item in connection.execute(
                "SELECT filename FROM schema_migrations ORDER BY filename"
            ).fetchall()
        )
        return integrity, migrations
    except BackupError:
        raise
    except (OSError, RuntimeError, sqlite3.Error) as error:
        raise BackupError("backup_member_invalid") from error
    finally:
        if connection is not None:
            connection.close()


def _known_migrations() -> frozenset[str]:
    return frozenset(path.name for path in MIGRATIONS_DIR.glob("*.sql"))


def _load_manifest(backup_dir: Path) -> BackupManifest:
    try:
        if backup_dir.is_symlink() or not backup_dir.is_dir():
            raise BackupError("backup_manifest_invalid")
    except BackupError:
        raise
    except (OSError, RuntimeError) as error:
        raise BackupError("backup_manifest_invalid") from error
    manifest_path = backup_dir / MANIFEST_FILENAME
    _safe_file(manifest_path, error_code="backup_manifest_invalid")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = BackupManifest.from_dict(payload)
    except BackupError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BackupError("backup_manifest_invalid") from error
    if manifest.format_version != BACKUP_FORMAT_VERSION or manifest.status != "complete":
        raise BackupError("backup_schema_incompatible")
    if _manifest_digest(manifest) != manifest.manifest_sha256:
        raise BackupError("backup_digest_mismatch")
    marker = backup_dir / COMPLETION_FILENAME
    _safe_file(marker, error_code="backup_manifest_invalid")
    try:
        if marker.read_text(encoding="utf-8").strip() != manifest.backup_id:
            raise BackupError("backup_manifest_invalid")
    except (OSError, UnicodeError) as error:
        raise BackupError("backup_manifest_invalid") from error
    if {member.name for member in manifest.members} != REQUIRED_MEMBERS:
        raise BackupError("backup_manifest_invalid")
    if {member.filename for member in manifest.members} != set(MEMBER_FILENAMES.values()):
        raise BackupError("backup_manifest_invalid")
    if any(
        MEMBER_FILENAMES.get(member.name) != member.filename or member.source_label != member.name
        for member in manifest.members
    ):
        raise BackupError("backup_manifest_invalid")
    return manifest


def _verify_staged_member(
    path: Path, member: BackupMember, known_migrations: frozenset[str]
) -> None:
    try:
        _safe_file(path, error_code="restore_staging_failed")
        if path.stat().st_size != member.size or _sha256(path) != member.sha256:
            raise BackupError("restore_staging_failed")
        integrity, migrations = _read_sqlite_metadata(path)
        if integrity != member.integrity_check or migrations != member.schema_migrations:
            raise BackupError("restore_staging_failed")
        if not set(migrations).issubset(known_migrations):
            raise BackupError("restore_staging_failed")
    except BackupError as error:
        if error.code == "restore_staging_failed":
            raise
        raise BackupError("restore_staging_failed") from error
    except (OSError, sqlite3.Error) as error:
        raise BackupError("restore_staging_failed") from error


class BackupService:
    """Create, verify, and restore a local runtime backup set."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()

    async def create(
        self,
        *,
        sources: tuple[BackupSource, ...],
        output_dir: Path,
        deployment_id: str,
    ) -> BackupManifest:
        self._validate_sources(sources, output_dir=output_dir, deployment_id=deployment_id)
        output = _safe_directory(output_dir, error_code="backup_destination_invalid")
        now = self._clock.now().astimezone(UTC)
        backup_id = f"{now:%Y%m%dT%H%M%S%fZ}-{uuid4().hex[:12]}"
        temporary = output / f".{backup_id}.tmp"
        final = output / backup_id
        try:
            temporary.mkdir()
            results = await asyncio.gather(
                *(self._backup_member(source, temporary) for source in sources),
                return_exceptions=True,
            )
            member_results: list[BackupMember] = []
            for result in results:
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, BackupError):
                    raise result
                if isinstance(result, BaseException):
                    raise BackupError("backup_source_unavailable") from result
                if not isinstance(result, BackupMember):
                    raise BackupError("backup_source_unavailable")
                member_results.append(result)
            members = tuple(sorted(member_results, key=lambda member: member.name))
            manifest = BackupManifest(
                backup_id=backup_id,
                format_version=BACKUP_FORMAT_VERSION,
                deployment_id=deployment_id,
                created_at=now.isoformat(),
                completed_at=self._clock.now().astimezone(UTC).isoformat(),
                members=members,
                manifest_sha256="",
                status="complete",
            )
            manifest = replace(manifest, manifest_sha256=_manifest_digest(manifest))
            manifest_path = temporary / MANIFEST_FILENAME
            manifest_path.write_bytes(_canonical_json(manifest.to_dict()))
            _fsync_file(manifest_path)
            marker = temporary / COMPLETION_FILENAME
            marker.write_text(f"{backup_id}\n", encoding="utf-8")
            _fsync_file(marker)
            _fsync_directory(temporary)
            os.replace(temporary, final)
            _fsync_directory(output)
            return manifest
        except asyncio.CancelledError:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        except BackupError:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        except (OSError, sqlite3.Error) as error:
            shutil.rmtree(temporary, ignore_errors=True)
            raise BackupError("backup_destination_invalid") from error

    async def _backup_member(self, source: BackupSource, temporary: Path) -> BackupMember:
        started = self._clock.now().astimezone(UTC)
        destination = temporary / MEMBER_FILENAMES[source.name]
        try:
            if source.storage is None:
                result = await _to_thread_drain(source.database.backup_to, destination)
            else:
                result = await source.storage.run(lambda: source.database.backup_to(destination))
            if not isinstance(result, SQLiteBackupResult):
                raise RuntimeError("invalid backup result")
            await _to_thread_drain(_fsync_file, destination)
            size = await _to_thread_drain(lambda: destination.stat().st_size)
            digest = await _to_thread_drain(_sha256, destination)
        except BackupError:
            raise
        except (OSError, sqlite3.Error, RuntimeError) as error:
            raise BackupError("backup_source_unavailable") from error
        completed = self._clock.now().astimezone(UTC)
        return BackupMember(
            name=source.name,
            filename=MEMBER_FILENAMES[source.name],
            source_label=source.name,
            started_at=started.isoformat(),
            completed_at=completed.isoformat(),
            size=size,
            sha256=digest,
            integrity_check=result.integrity_check,
            schema_migrations=result.schema_migrations,
        )

    @staticmethod
    def _validate_sources(
        sources: tuple[BackupSource, ...], *, output_dir: Path, deployment_id: str
    ) -> None:
        if not deployment_id or "\n" in deployment_id:
            raise BackupError("backup_destination_invalid")
        if {source.name for source in sources} != REQUIRED_MEMBERS or len(sources) != 2:
            raise BackupError("backup_source_unavailable")
        database_paths: set[Path] = set()
        try:
            output_resolved = output_dir.resolve()
        except (OSError, RuntimeError) as error:
            raise BackupError("backup_destination_invalid") from error
        for source in sources:
            if source.name not in REQUIRED_MEMBERS:
                raise BackupError("backup_source_unavailable")
            database_path = source.database.path
            try:
                if database_path.is_symlink() or not database_path.is_file():
                    raise BackupError("backup_source_unavailable")
                resolved_database_path = database_path.resolve()
            except BackupError:
                raise
            except (OSError, RuntimeError) as error:
                raise BackupError("backup_source_unavailable") from error
            if resolved_database_path in database_paths:
                raise BackupError("backup_source_unavailable")
            database_paths.add(resolved_database_path)
            source_parent = resolved_database_path.parent
            if output_resolved == source_parent or source_parent in output_resolved.parents:
                raise BackupError("backup_destination_invalid")

    def verify(self, backup_dir: Path) -> BackupManifest:
        manifest = _load_manifest(backup_dir)
        known_migrations = _known_migrations()
        for member in manifest.members:
            path = backup_dir / member.filename
            try:
                if path.resolve().parent != backup_dir.resolve():
                    raise BackupError("backup_manifest_invalid")
            except (OSError, RuntimeError) as error:
                raise BackupError("backup_member_invalid") from error
            try:
                _safe_file(path, error_code="backup_member_invalid")
                if path.stat().st_size != member.size:
                    raise BackupError("backup_digest_mismatch")
                if _sha256(path) != member.sha256:
                    raise BackupError("backup_digest_mismatch")
                integrity, migrations = _read_sqlite_metadata(path)
                if integrity != member.integrity_check or migrations != member.schema_migrations:
                    raise BackupError("backup_schema_incompatible")
                if not set(migrations).issubset(known_migrations):
                    raise BackupError("backup_schema_incompatible")
            except BackupError:
                raise
            except (OSError, RuntimeError, sqlite3.Error) as error:
                raise BackupError("backup_member_invalid") from error
        return manifest

    async def restore(
        self,
        *,
        backup_dir: Path,
        target_data_dir: Path,
        deployment_id: str,
    ) -> RestoreResult:
        manifest = self.verify(backup_dir)
        if manifest.deployment_id != deployment_id:
            raise BackupError("backup_manifest_invalid")
        try:
            backup_resolved = backup_dir.resolve()
            target_exists = target_data_dir.exists()
            if target_exists and target_data_dir.is_symlink():
                raise BackupError("restore_staging_failed")
            target_resolved = target_data_dir.resolve()
            if target_resolved == backup_resolved or backup_resolved in target_resolved.parents:
                raise BackupError("restore_staging_failed")
            target_data_dir.parent.mkdir(parents=True, exist_ok=True)
        except BackupError:
            raise
        except (OSError, RuntimeError) as error:
            raise BackupError("restore_staging_failed") from error

        target_lock = SQLiteAdvisoryLock(target_data_dir / MEMBER_FILENAMES["operational"])
        try:
            target_lock.acquire(blocking=False)
        except (BlockingIOError, OSError) as error:
            raise BackupError("restore_ownership_active") from error
        staging: Path | None = None
        rollback: Path | None = None
        try:
            self._assert_target_ownership_safe(target_data_dir / MEMBER_FILENAMES["operational"])
            staging = Path(
                tempfile.mkdtemp(
                    prefix=f".{target_data_dir.name}.restore-", dir=target_data_dir.parent
                )
            )
            for member in manifest.members:
                source = backup_dir / member.filename
                destination = staging / member.filename
                shutil.copy2(source, destination)
                _safe_file(destination, error_code="restore_staging_failed")
            known_migrations = _known_migrations()
            for member in manifest.members:
                _verify_staged_member(staging / member.filename, member, known_migrations)
            await self._migrate_staged_members(staging, manifest)
            rollback = self._copy_target_for_rollback(target_data_dir, manifest.backup_id)
            self._replace_target(target_data_dir, staging, rollback, manifest)
            if rollback is not None and not any(rollback.iterdir()):
                rollback.rmdir()
                rollback = None
            return RestoreResult(
                backup_id=manifest.backup_id,
                deployment_id=deployment_id,
                rollback_directory=rollback.name if rollback is not None else None,
            )
        except BackupError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise BackupError("restore_staging_failed") from error
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
            target_lock.release()

    async def _migrate_staged_members(self, staging: Path, manifest: BackupManifest) -> None:
        for member in manifest.members:
            database = SQLiteDatabase(staging / member.filename)
            try:
                await database.initialize()
            except (OSError, sqlite3.Error, RuntimeError) as error:
                raise BackupError("restore_staging_failed") from error
            finally:
                await database.close()
        self._reset_staged_ownership(staging / MEMBER_FILENAMES["operational"])

    def _reset_staged_ownership(self, path: Path) -> None:
        """Do not carry the source process's ephemeral lease into a restore."""

        uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=rw"
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(uri, uri=True)
            has_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_ownership'"
            ).fetchone()
            if has_table:
                connection.execute(
                    """UPDATE runtime_ownership
                       SET status = 'released', released_at = ?, uncertain = 0
                       WHERE status != 'released' OR uncertain != 0""",
                    (self._clock.now().astimezone(UTC).isoformat(),),
                )
                connection.commit()
        except (OSError, sqlite3.Error) as error:
            raise BackupError("restore_staging_failed") from error
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _assert_target_ownership_safe(path: Path) -> None:
        if not path.exists():
            return
        _safe_file(path, error_code="restore_staging_failed")
        uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=rw"
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=0.5)
            connection.execute("BEGIN IMMEDIATE")
            has_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_ownership'"
            ).fetchone()
            if has_table:
                rows = connection.execute(
                    "SELECT status, uncertain FROM runtime_ownership",
                ).fetchall()
                if any(status != "released" or bool(uncertain) for status, uncertain in rows):
                    raise BackupError("restore_ownership_active")
            connection.rollback()
        except BackupError:
            if connection is not None:
                connection.rollback()
            raise
        except (OSError, sqlite3.Error) as error:
            raise BackupError("restore_ownership_active") from error
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _copy_target_for_rollback(target: Path, backup_id: str) -> Path:
        rollback = Path(
            tempfile.mkdtemp(prefix=f".{target.name}.rollback-{backup_id}-", dir=target.parent)
        )
        try:
            if target.exists():
                if target.is_symlink() or not target.is_dir():
                    raise BackupError("restore_staging_failed")
                for member_filename in MEMBER_FILENAMES.values():
                    for suffix in ("", "-wal", "-shm"):
                        candidate = target / f"{member_filename}{suffix}"
                        if candidate.exists() or candidate.is_symlink():
                            _safe_file(candidate, error_code="restore_staging_failed")
                            shutil.copy2(candidate, rollback / candidate.name)
            return rollback
        except BackupError:
            shutil.rmtree(rollback, ignore_errors=True)
            raise
        except OSError as error:
            shutil.rmtree(rollback, ignore_errors=True)
            raise BackupError("restore_staging_failed") from error

    @staticmethod
    def _replace_target(
        target: Path, staging: Path, rollback: Path, manifest: BackupManifest
    ) -> None:
        applied: list[Path] = []
        try:
            target.mkdir(parents=True, exist_ok=True)
            for member in manifest.members:
                destination = target / member.filename
                os.replace(staging / member.filename, destination)
                applied.append(destination)
                for suffix in ("-wal", "-shm"):
                    sidecar = target / f"{member.filename}{suffix}"
                    if sidecar.exists() or sidecar.is_symlink():
                        sidecar.unlink()
            _fsync_directory(target)
        except (OSError, sqlite3.Error) as error:
            if rollback is not None:
                BackupService._rollback_target(target, rollback)
            else:
                for destination in applied:
                    try:
                        destination.unlink(missing_ok=True)
                    except OSError:
                        pass
            raise BackupError("restore_replacement_failed") from error

    @staticmethod
    def _rollback_target(target: Path, rollback: Path) -> None:
        for member_filename in MEMBER_FILENAMES.values():
            for suffix in ("", "-wal", "-shm"):
                name = f"{member_filename}{suffix}"
                original = rollback / name
                destination = target / name
                try:
                    if original.exists():
                        shutil.copy2(original, destination)
                    else:
                        destination.unlink(missing_ok=True)
                except OSError as error:
                    raise BackupError("restore_replacement_failed") from error
