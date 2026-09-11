"""Validated SQLite backups and isolated administrative PostgreSQL archives."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote
from uuid import uuid4

import psycopg
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from domoai.domain.household_preferences import (
    HouseholdPreference,
    PreferenceHistoryEntry,
    PreferenceResult,
    PreferenceVersion,
)
from domoai.persistence.contract_compatibility import check_runtime_floor
from domoai.persistence.household_preferences import rotate_restored_preference_generations
from domoai.persistence.postgres import (
    _POSTGRES_SCHEMA_VERSION,
    _SCHEMA_LOCK_KEY,
    _PostgresConnectionProxy,
    check_restore_quarantine,
)
from domoai.persistence.serialized import SerializedStorageExecutor
from domoai.persistence.sqlite import (
    MIGRATIONS_DIR,
    SQLiteAdvisoryLock,
    SQLiteBackupResult,
    SQLiteDatabase,
)
from domoai.runtime.clock import Clock, SystemClock

BACKUP_FORMAT_VERSION = 2
SUPPORTED_BACKUP_FORMAT_VERSIONS = frozenset({1, BACKUP_FORMAT_VERSION})
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


def _run_postgres_tool(
    command: tuple[str, ...],
    dsn: str | None = None,
    data: bytes | None = None,
) -> bytes:
    """Keep conninfo out of argv and never surface tool stderr or exceptions."""
    try:
        environment = os.environ.copy()
        if dsn is not None:
            variables = {
                option.keyword.decode(): option.envvar.decode()
                for option in psycopg.pq.Conninfo.get_defaults()
                if option.envvar
            }
            for key, value in conninfo_to_dict(dsn).items():
                # Parsing a DSN without keyword overrides yields decoded strings.
                environment[variables[key]] = cast(str, value)
        result = subprocess.run(
            command,
            input=data,
            capture_output=True,
            env=environment,
            check=True,
            timeout=300,
        )
        return result.stdout
    except Exception:
        raise BackupError("postgres_backup_tool_failed") from None


class PostgresBackupService:
    """Offline administrative logical archives; separate from SQLite manifests.

    The caller provides an isolated, newly created database and controls its
    administrative credentials. This is not an operational restore protocol or
    permission to connect restored state to physical adapters. Archives contain
    full database data and require protected local storage.
    """

    def __init__(self, *, runner: Callable[..., bytes] = _run_postgres_tool) -> None:
        self._runner = runner

    @staticmethod
    def _content(connection: Any) -> dict[str, Any]:
        """Hash every user table in one exported PostgreSQL snapshot."""
        tables = connection.execute(
            "SELECT n.nspname, c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE c.relkind IN ('r','p') AND n.nspname !~ '^pg_' "
            "AND n.nspname NOT IN ('information_schema','domoai_restore_guard') "
            "ORDER BY 1, 2"
        ).fetchall()
        content = {}
        for namespace, table in tables:
            rows = connection.execute(
                sql.SQL("SELECT row_to_json(t)::text FROM {} AS t").format(
                    sql.Identifier(namespace, table)
                )
            ).fetchall()
            digest = hashlib.sha256()
            for row in sorted(row[0] for row in rows):
                encoded = row.encode("utf-8")
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
            content[f"{namespace}.{table}"] = {"rows": len(rows), "sha256": digest.hexdigest()}
        return content

    def create(self, *, source_dsn: str, output_dir: Path) -> None:
        staging: Path | None = None
        try:
            if output_dir.exists():
                raise BackupError("postgres_backup_destination_exists")
            output_dir.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=".postgres-backup-", dir=output_dir.parent))
            with psycopg.connect(source_dsn) as connection:
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                check_restore_quarantine(connection)
                snapshot = cast(
                    tuple[str], connection.execute("SELECT pg_export_snapshot()").fetchone()
                )[0]
                content = self._content(connection)
                payload = self._runner(
                    (
                        "pg_dump",
                        "--format=custom",
                        "--no-owner",
                        "--no-privileges",
                        "--exclude-schema=domoai_restore_guard",
                        f"--snapshot={snapshot}",
                    ),
                    source_dsn,
                )
            if not payload.startswith(b"PGDMP"):
                raise BackupError("postgres_backup_invalid_archive")
            self._runner(("pg_restore", "--list"), None, payload)
            metadata = {
                "format": "domoai-postgresql-custom",
                "version": 1,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "tables": content,
            }
            for name, data in (
                ("database.dump", payload),
                (
                    "postgres-manifest.json",
                    json.dumps(metadata, sort_keys=True).encode(),
                ),
            ):
                with (staging / name).open("xb") as member:
                    os.chmod(staging / name, 0o600)
                    member.write(data)
                    member.flush()
                    os.fsync(member.fileno())
            _fsync_directory(staging)
            staging.rename(output_dir)
            staging = None
            _fsync_directory(output_dir.parent)
        except BackupError:
            raise
        except Exception:
            raise BackupError("postgres_backup_failed") from None
        finally:
            if staging is not None:
                shutil.rmtree(staging)

    def _verify(self, backup_dir: Path) -> tuple[bytes, dict[str, Any]]:
        metadata = json.loads((backup_dir / "postgres-manifest.json").read_text())
        payload = (backup_dir / "database.dump").read_bytes()
        if (
            not isinstance(metadata, dict)
            or set(metadata) != {"format", "version", "size", "sha256", "tables"}
            or metadata["format"] != "domoai-postgresql-custom"
            or type(metadata["version"]) is not int
            or metadata["version"] != 1
            or type(metadata["size"]) is not int
            or metadata["size"] != len(payload)
            or metadata["sha256"] != hashlib.sha256(payload).hexdigest()
            or not payload.startswith(b"PGDMP")
            or not isinstance(metadata["tables"], dict)
        ):
            raise BackupError("postgres_backup_invalid_archive")
        self._runner(("pg_restore", "--list"), None, payload)
        return payload, metadata

    @staticmethod
    def _validate_preferences(connection: Any) -> None:
        heads = {}
        for tenant, household, generation, revision in connection.execute(
            "SELECT tenant_id, household_id, generation, revision FROM household_preference_heads"
        ):
            heads[tenant, household] = PreferenceVersion(generation=generation, revision=revision)
        slots = {}
        for tenant, household, identity, key, kind, target, payload in connection.execute(
            "SELECT tenant_id, household_id, preference_id, key, target_kind, target_id, payload "
            "FROM household_preferences"
        ):
            slot = HouseholdPreference.model_validate_json(payload)
            if (
                str(slot.preference_id) != identity
                or slot.key != key
                or slot.target.kind != kind
                or slot.target.id != target
                or not 1 <= slot.version.revision <= heads[tenant, household].revision
            ):
                raise BackupError("postgres_backup_invalid_preferences")
            slots[tenant, household, identity] = slot
        history = {}
        for tenant, household, revision, identity, payload in connection.execute(
            "SELECT tenant_id, household_id, revision, preference_id, payload "
            "FROM household_preference_history"
        ):
            entry = PreferenceHistoryEntry.model_validate_json(payload)
            if (
                entry.version.revision != revision
                or str(entry.preference_id) != identity
                or not 1 <= revision <= heads[tenant, household].revision
                or (tenant, household, identity) not in slots
                or (
                    entry.snapshot is not None
                    and (
                        entry.snapshot.preference_id != entry.preference_id
                        or entry.snapshot.version != entry.version
                    )
                )
            ):
                raise BackupError("postgres_backup_invalid_preferences")
            history[tenant, household, revision] = entry
        for scope, head in heads.items():
            if sum(key[:2] == scope for key in history) != head.revision:
                raise BackupError("postgres_backup_invalid_preferences")
        for (tenant, household, _), slot in slots.items():
            entry = history[tenant, household, slot.version.revision]
            if entry.preference_id != slot.preference_id or (
                entry.snapshot != slot and not (slot.erased and entry.snapshot is None)
            ):
                raise BackupError("postgres_backup_invalid_preferences")
        receipts = set()
        for tenant, household, generation, identity, payload in connection.execute(
            "SELECT tenant_id, household_id, generation, preference_id, response "
            "FROM household_preference_requests"
        ):
            result = PreferenceResult.model_validate_json(payload)
            history_key = (tenant, household, result.version.revision)
            entry = history[history_key]
            if (
                str(result.version.generation) != generation
                or result.version != entry.version
                or result.operation != entry.operation
                or str(entry.preference_id) != identity
                or result.preference != entry.snapshot
            ):
                raise BackupError("postgres_backup_invalid_preferences")
            receipts.add(history_key)
        if receipts != set(history):
            raise BackupError("postgres_backup_invalid_preferences")

    def restore(self, *, backup_dir: Path, target_dsn: str) -> None:
        try:
            with psycopg.connect(target_dsn) as connection:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))", (_SCHEMA_LOCK_KEY,)
                )
                # No retry into partially imported state, including an empty
                # failed import whose only durable object is this guard.
                occupied = connection.execute(
                    "SELECT 1 FROM pg_namespace WHERE nspname !~ '^pg_' "
                    "AND nspname NOT IN ('public','information_schema') "
                    # Schema dependencies include collations, operators and
                    # text-search objects as well as tables/functions/types.
                    "UNION ALL SELECT 1 FROM pg_depend "
                    "WHERE refclassid='pg_namespace'::regclass "
                    "AND refobjid='public'::regnamespace LIMIT 1"
                ).fetchone()
                if occupied is not None:
                    raise BackupError("postgres_restore_target_not_empty")
                connection.execute(
                    "CREATE SCHEMA domoai_restore_guard; "
                    "CREATE TABLE domoai_restore_guard.status "
                    "(state TEXT NOT NULL CHECK (state IN ('restoring','ready'))); "
                    "INSERT INTO domoai_restore_guard.status VALUES ('restoring')"
                )
            payload, metadata = self._verify(backup_dir)
            self._runner(
                (
                    "pg_restore",
                    "--exit-on-error",
                    "--single-transaction",
                    "--no-owner",
                    "--no-privileges",
                    "--dbname=",
                    "--exclude-schema=domoai_restore_guard",
                ),
                target_dsn,
                payload,
            )
            with psycopg.connect(target_dsn) as connection:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))", (_SCHEMA_LOCK_KEY,)
                )
                check_runtime_floor(connection, postgres=True)
                if (
                    connection.execute(
                        "SELECT 1 FROM schema_migrations WHERE filename=%s",
                        (_POSTGRES_SCHEMA_VERSION,),
                    ).fetchone()
                    is None
                ):
                    raise BackupError("postgres_restore_schema_incompatible")
                before = self._content(connection)
                if before != metadata["tables"]:
                    raise BackupError("postgres_restore_content_mismatch")
                self._validate_preferences(connection)
                heads = connection.execute(
                    "SELECT tenant_id, household_id, generation, revision "
                    "FROM household_preference_heads ORDER BY 1, 2"
                ).fetchall()
                rotate_restored_preference_generations(_PostgresConnectionProxy(connection))
                rotated = connection.execute(
                    "SELECT tenant_id, household_id, generation, revision "
                    "FROM household_preference_heads ORDER BY 1, 2"
                ).fetchall()
                if len(heads) != len(rotated) or any(
                    old[:2] != new[:2] or old[2] == new[2] or old[3] != new[3]
                    for old, new in zip(heads, rotated, strict=True)
                ):
                    raise BackupError("postgres_restore_generation_failed")
                self._validate_preferences(connection)
                after = self._content(connection)
                before.pop("public.household_preference_heads", None)
                after.pop("public.household_preference_heads", None)
                if before != after:
                    raise BackupError("postgres_restore_content_mismatch")
                connection.execute("UPDATE domoai_restore_guard.status SET state='ready'")
        except BackupError:
            raise
        except Exception:
            raise BackupError("postgres_restore_failed") from None


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
    encryption: str = "none"
    nonce: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
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
        if self.encryption != "none" or self.nonce is not None:
            payload["encryption"] = self.encryption
            payload["nonce"] = self.nonce
        return payload

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
        if not required.issubset(payload) or set(payload) - required - {"encryption", "nonce"}:
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
        encryption = payload.get("encryption", "none")
        nonce = payload.get("nonce")
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
            or encryption not in {"none", "aes-256-gcm"}
            or (encryption == "none" and nonce is not None)
            or (encryption == "aes-256-gcm" and not isinstance(nonce, str))
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
            encryption=encryption,
            nonce=nonce,
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
    if (
        manifest.format_version not in SUPPORTED_BACKUP_FORMAT_VERSIONS
        or manifest.status != "complete"
    ):
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
        if member.encryption == "none" and (
            path.stat().st_size != member.size or _sha256(path) != member.sha256
        ):
            raise BackupError("restore_staging_failed")
        _verify_sqlite_member(path, member, known_migrations, error_code="restore_staging_failed")
    except BackupError as error:
        if error.code == "restore_staging_failed":
            raise
        raise BackupError("restore_staging_failed") from error


def _verify_sqlite_member(
    path: Path,
    member: BackupMember,
    known_migrations: frozenset[str],
    *,
    error_code: str,
) -> None:
    try:
        integrity, migrations = _read_sqlite_metadata(path)
        if integrity != member.integrity_check or migrations != member.schema_migrations:
            raise BackupError(error_code)
        if not set(migrations).issubset(known_migrations):
            raise BackupError(error_code)
    except BackupError:
        raise
    except (OSError, sqlite3.Error, RuntimeError) as error:
        raise BackupError(error_code) from error


def _validate_encryption_key(key: bytes | None) -> bytes | None:
    if key is not None and len(key) != 32:
        raise BackupError("backup_encryption_key_invalid")
    return key


def load_backup_encryption_key(path: Path) -> bytes:
    """Load exactly one AES-256 key from a separate, owner-readable file."""

    try:
        if path.is_symlink() or not path.is_file():
            raise BackupError("backup_encryption_key_invalid")
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise BackupError("backup_encryption_key_invalid")
        key = path.read_bytes()
    except BackupError:
        raise
    except OSError as error:
        raise BackupError("backup_encryption_key_invalid") from error
    return _validate_encryption_key(key) or b""


def _encrypt_member(source: Path, destination: Path, *, key: bytes, filename: str) -> str:
    nonce = os.urandom(12)
    try:
        plaintext = source.read_bytes()
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, filename.encode("utf-8"))
        destination.write_bytes(ciphertext)
        _fsync_file(destination)
    except (OSError, ValueError) as error:
        raise BackupError("backup_encryption_failed") from error
    return base64.urlsafe_b64encode(nonce).decode("ascii")


def _decrypt_member(source: Path, destination: Path, *, key: bytes, member: BackupMember) -> None:
    if member.encryption != "aes-256-gcm" or member.nonce is None:
        raise BackupError("backup_schema_incompatible")
    try:
        nonce = base64.urlsafe_b64decode(member.nonce.encode("ascii"))
        if len(nonce) != 12:
            raise ValueError("invalid AES-GCM nonce")
        plaintext = AESGCM(key).decrypt(nonce, source.read_bytes(), member.filename.encode("utf-8"))
        destination.write_bytes(plaintext)
        _fsync_file(destination)
    except (InvalidTag, OSError, ValueError) as error:
        raise BackupError("backup_decryption_failed") from error


class BackupService:
    """Create, verify, and restore a local runtime backup set."""

    def __init__(self, *, clock: Clock | None = None, encryption_key: bytes | None = None) -> None:
        self._clock = clock or SystemClock()
        self._encryption_key = _validate_encryption_key(encryption_key)

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
        plaintext_destination = temporary / f".{destination.name}.plaintext"
        encryption = "none"
        nonce: str | None = None
        try:
            if source.storage is None:
                result = await _to_thread_drain(source.database.backup_to, plaintext_destination)
            else:
                result = await source.storage.run(
                    lambda: source.database.backup_to(plaintext_destination)
                )
            if not isinstance(result, SQLiteBackupResult):
                raise RuntimeError("invalid backup result")
            encryption_key = self._encryption_key
            if encryption_key is not None:
                nonce = await _to_thread_drain(
                    lambda: _encrypt_member(
                        plaintext_destination,
                        destination,
                        key=encryption_key,
                        filename=destination.name,
                    )
                )
                encryption = "aes-256-gcm"
                plaintext_destination.unlink(missing_ok=True)
            else:
                os.replace(plaintext_destination, destination)
            await _to_thread_drain(_fsync_file, destination)
            size = await _to_thread_drain(lambda: destination.stat().st_size)
            digest = await _to_thread_drain(_sha256, destination)
        except BackupError:
            raise
        except (OSError, sqlite3.Error, RuntimeError) as error:
            raise BackupError("backup_source_unavailable") from error
        finally:
            plaintext_destination.unlink(missing_ok=True)
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
            encryption=encryption,
            nonce=nonce,
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
        temporary_directory: Path | None = None
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
                verification_path = path
                if member.encryption == "aes-256-gcm":
                    if self._encryption_key is None:
                        raise BackupError("backup_encryption_key_required")
                    if temporary_directory is None:
                        temporary_directory = Path(
                            tempfile.mkdtemp(prefix=".backup-verify-", dir=backup_dir)
                        )
                    verification_path = temporary_directory / member.filename
                    _decrypt_member(
                        path,
                        verification_path,
                        key=self._encryption_key,
                        member=member,
                    )
                _verify_sqlite_member(
                    verification_path,
                    member,
                    known_migrations,
                    error_code="backup_schema_incompatible",
                )
            except BackupError:
                if temporary_directory is not None:
                    shutil.rmtree(temporary_directory, ignore_errors=True)
                raise
            except (OSError, RuntimeError, sqlite3.Error) as error:
                if temporary_directory is not None:
                    shutil.rmtree(temporary_directory, ignore_errors=True)
                raise BackupError("backup_member_invalid") from error
        if temporary_directory is not None:
            shutil.rmtree(temporary_directory, ignore_errors=True)
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
                if member.encryption == "aes-256-gcm":
                    if self._encryption_key is None:
                        raise BackupError("backup_encryption_key_required")
                    encrypted_staging = staging / f".{member.filename}.encrypted"
                    shutil.copy2(source, encrypted_staging)
                    _decrypt_member(
                        encrypted_staging,
                        destination,
                        key=self._encryption_key,
                        member=member,
                    )
                    encrypted_staging.unlink(missing_ok=True)
                else:
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
                # Staging is offline and fully migrated. Fence old client CAS
                # tokens before this file can replace a served database.
                rotate_restored_preference_generations(database.connection)
                database.connection.commit()
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
