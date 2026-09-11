"""Encrypted local secret storage and reference-only resolution.

The control plane deliberately keeps this module outside domain and runtime
models. Callers receive a reference or a resolved value at the narrowest
possible boundary; persisted connection profiles never receive the value.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets as random_secrets
import stat
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast, runtime_checkable
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from domoai.domain.coordination import LeaseScope

_ENV_REFERENCE = re.compile(r"^env:([A-Za-z_][A-Za-z0-9_]*)$")
_SECRET_REFERENCE = re.compile(r"^secret:([0-9a-fA-F-]{36})$")
_KEY_BYTES = 32
_NONCE_BYTES = 12
_PAYLOAD_VERSION = 1


class SecretStoreError(Exception):
    """Base error whose message is safe to expose to operators."""


class SecretReferenceError(SecretStoreError):
    """The reference format is not supported."""


class SecretUnavailableError(SecretStoreError):
    """A valid reference cannot currently be resolved."""


class SecretPermissionError(SecretStoreError):
    """A key or encrypted record has unsafe filesystem permissions."""


class SecretKeyError(SecretStoreError):
    """The local master key cannot be used."""


class SecretResolverPort(Protocol):
    """Resolve an opaque reference at an explicitly authorized boundary."""

    def resolve(self, reference: str) -> str: ...


class SecretStorePort(SecretResolverPort, Protocol):
    """Store and remove secret material without exposing it in metadata."""

    def put(self, value: str) -> str: ...

    def delete(self, reference: str) -> None: ...


@runtime_checkable
class TemporarySecretStorePort(SecretResolverPort, Protocol):
    """Optional owned memory capability; legacy resolver/store ports stay unchanged."""

    def put_temporary(self, value: str, *, owner: object) -> str: ...

    def close_temporary(self, owner: object) -> None: ...

    def is_temporary(self, reference: str) -> bool: ...

    def prepare_temporary(self, reference: str, *, owner: object) -> PreparedSecret: ...

    def materialize(self, prepared: PreparedSecret) -> None: ...

    def release_temporary(self, reference: str, *, owner: object) -> None: ...

    def matches_secret(self, reference: str, expected_digest: str) -> bool: ...

    def discard_prepared(self, prepared: PreparedSecret) -> bool: ...


@dataclass(frozen=True)
class PreparedSecret:
    reference: str
    envelope: bytes = field(repr=False)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.envelope).hexdigest()


@dataclass
class _TemporaryCredential:
    value: str = field(repr=False)
    owner: object = field(repr=False)
    expires_at: float
    closed: bool = False
    pins: int = 0
    prepared: PreparedSecret | None = field(default=None, repr=False)


class _WindowsFileLock(Protocol):
    """msvcrt's Windows API, absent from the POSIX typeshed module stub."""

    LK_LOCK: int
    LK_UNLCK: int

    def locking(self, descriptor: int, mode: int, size: int) -> None: ...


class FileSecretStore(SecretStorePort):
    """Owner-readable AES-GCM secret store backed by local files."""

    key_version = _PAYLOAD_VERSION

    def __init__(
        self,
        master_key_path: Path,
        *,
        secret_dir: Path | None = None,
        scope: LeaseScope | None = None,
        environment: Mapping[str, str] | None = None,
        create_key: bool = True,
    ) -> None:
        self.master_key_path = Path(master_key_path)
        self.secret_dir = secret_dir or self.master_key_path.parent / "secrets"
        self._scope = (scope.tenant_id, scope.household_id, scope.deployment_id) if scope else None
        self._environment = dict(environment or {})
        self._create_key = create_key
        self._key = self._load_or_create_key()
        self._temporary: dict[str, _TemporaryCredential] = {}

    def __repr__(self) -> str:
        return (
            f"FileSecretStore(master_key_path={str(self.master_key_path)!r}, "
            f"secret_dir={str(self.secret_dir)!r})"
        )

    @property
    def scope(self) -> LeaseScope | None:
        if self._scope is None:
            return None
        return LeaseScope(
            tenant_id=self._scope[0], household_id=self._scope[1], deployment_id=self._scope[2]
        )

    @contextmanager
    def lock_references(self, references: Sequence[str]) -> Iterator[None]:
        """Hold stable, cross-process UUID locks through an offline DB transaction."""

        with ExitStack() as locks:
            for identifier in sorted({self._parse_secret_reference(ref) for ref in references}):
                locks.enter_context(self._locked(identifier))
            yield

    def copy_legacy(
        self,
        reference: str,
        *,
        expected_digest: str,
        target_reference: str,
        dry_run: bool = False,
    ) -> None:
        """Explicit offline adoption; preserve source bytes and never clobber a target."""

        with ExitStack() as locks:
            if not dry_run:
                locks.enter_context(self.lock_references((reference, target_reference)))
            self._copy_legacy_locked(
                reference,
                expected_digest=expected_digest,
                target_reference=target_reference,
                dry_run=dry_run,
            )

    def _copy_legacy_locked(
        self,
        reference: str,
        *,
        expected_digest: str,
        target_reference: str,
        dry_run: bool,
    ) -> None:
        source_id = self._parse_secret_reference(reference)
        target_id = self._parse_secret_reference(target_reference)
        if self._scope is None or source_id == target_id:
            raise SecretStoreError(
                "secret adoption requires distinct source and scoped destination"
            )
        try:
            raw = self._path_for(source_id).read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected_digest:
                raise SecretStoreError("legacy secret changed after review")
            legacy = FileSecretStore(
                self.master_key_path, secret_dir=self.secret_dir, create_key=False
            )
            value = legacy._resolve_file(reference, source_id)
            if self._path_for(target_id).exists():
                if self._resolve_file(target_reference, target_id) != value:
                    raise SecretStoreError("secret adoption destination conflicts")
            elif not dry_run:
                self._write_value(target_id, value)
        except OSError:
            raise SecretUnavailableError("legacy secret is unavailable") from None

    def put(self, value: str) -> str:
        reference_id = uuid4()
        with self._locked(reference_id):
            self._write_value(reference_id, value)
        return f"secret:{reference_id}"

    def _purge_temporary(self) -> None:
        for reference, entry in tuple(self._temporary.items()):
            if entry.expires_at <= time.monotonic():
                entry.closed = True
            if entry.closed and not entry.pins:
                del self._temporary[reference]

    def put_temporary(self, value: str, *, owner: object) -> str:
        if not isinstance(value, str) or not value:
            raise SecretStoreError("secret value must be a non-empty string")
        self._purge_temporary()
        if len(self._temporary) >= 100:
            raise SecretStoreError("temporary credential capacity reached")
        reference = f"secret:{uuid4()}"
        self._temporary[reference] = _TemporaryCredential(value, owner, time.monotonic() + 900)
        return reference

    def close_temporary(self, owner: object) -> None:
        for entry in self._temporary.values():
            if entry.owner == owner:
                entry.closed = True
        self._purge_temporary()

    def is_temporary(self, reference: str) -> bool:
        self._purge_temporary()
        return reference in self._temporary

    def prepare_temporary(self, reference: str, *, owner: object) -> PreparedSecret:
        self._purge_temporary()
        entry = self._temporary.get(reference)
        if entry is None or entry.owner != owner or entry.closed:
            raise SecretUnavailableError("temporary credential is unavailable")
        if entry.prepared is None:
            payload = self._value_payload(self._parse_secret_reference(reference), entry.value)
            entry.prepared = PreparedSecret(
                reference, json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            )
        entry.pins = 1
        return entry.prepared

    def materialize(self, prepared: PreparedSecret) -> None:
        identifier = self._parse_secret_reference(prepared.reference)
        with self._locked(identifier):
            path = self._path_for(identifier)
            if path.exists():
                _require_owner_only(path)
                if path.read_bytes() != prepared.envelope:
                    raise SecretStoreError("encrypted secret conflicts")
                return
            self._atomic_write(path, json.loads(prepared.envelope))

    def release_temporary(self, reference: str, *, owner: object) -> None:
        entry = self._temporary.get(reference)
        if entry is not None and entry.owner == owner:
            del self._temporary[reference]

    def matches_secret(self, reference: str, expected_digest: str) -> bool:
        identifier = self._parse_secret_reference(reference)
        with self._locked(identifier):
            path = self._path_for(identifier)
            try:
                _require_owner_only(path)
                return hashlib.sha256(path.read_bytes()).hexdigest() == expected_digest
            except (OSError, SecretPermissionError):
                return False

    def discard_prepared(self, prepared: PreparedSecret) -> bool:
        identifier = self._parse_secret_reference(prepared.reference)
        with self._locked(identifier):
            path = self._path_for(identifier)
            if not path.exists():
                return True
            _require_owner_only(path)
            if hashlib.sha256(path.read_bytes()).hexdigest() != prepared.digest:
                return False
            path.unlink()
            _sync_directory(self.secret_dir)
            return True

    def _write_value(self, reference_id: UUID, value: str) -> None:
        self._atomic_write(self._path_for(reference_id), self._value_payload(reference_id, value))

    def _value_payload(self, reference_id: UUID, value: str) -> dict[str, object]:
        if not isinstance(value, str) or not value:
            raise SecretStoreError("secret value must be a non-empty string")
        reference = f"secret:{reference_id}"
        metadata: dict[str, object] = {
            "version": 2 if self._scope else 1,
            "key_version": self.key_version,
            "reference": str(reference_id),
        }
        if self._scope is not None:
            metadata["scope"] = dict(
                zip(("tenant_id", "household_id", "deployment_id"), self._scope, strict=True)
            )
        nonce = random_secrets.token_bytes(_NONCE_BYTES)
        ciphertext = AESGCM(self._key).encrypt(
            nonce, value.encode("utf-8"), self._aad(reference, metadata)
        )
        return {
            **metadata,
            "nonce": _encode(nonce),
            "ciphertext": _encode(ciphertext),
        }

    def resolve(self, reference: str) -> str:
        if not isinstance(reference, str):
            raise SecretReferenceError("secret reference is invalid")
        self._purge_temporary()
        temporary = self._temporary.get(reference)
        if temporary is not None:
            return temporary.value
        env_match = _ENV_REFERENCE.fullmatch(reference)
        if env_match is not None:
            name = env_match.group(1)
            value = self._environment.get(name) if self._scope else os.getenv(name)
            if value is None:
                raise SecretUnavailableError("environment secret is unavailable")
            return value
        secret_id = self._parse_secret_reference(reference)
        with self._locked(secret_id):
            return self._resolve_file(reference, secret_id)

    def _resolve_file(self, reference: str, secret_id: UUID) -> str:
        path = self._path_for(secret_id)
        if not path.exists():
            raise SecretUnavailableError("stored secret is unavailable")
        try:
            _require_owner_only(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
            expected_keys = {"version", "key_version", "reference", "nonce", "ciphertext"}
            if self._scope is not None:
                expected_keys.add("scope")
                if payload.get("version") != 2:
                    raise ValueError
                expected_scope = dict(
                    zip(("tenant_id", "household_id", "deployment_id"), self._scope, strict=True)
                )
                if payload.get("scope") != expected_scope:
                    raise ValueError
            elif payload.get("version") != 1:
                raise ValueError
            if self._scope is None and "key_version" not in payload:
                expected_keys.remove("key_version")
            if set(payload) != expected_keys or payload.get("key_version", 1) != self.key_version:
                raise ValueError
            if payload.get("reference") != str(secret_id):
                raise ValueError
            nonce, ciphertext = _decode(payload.get("nonce")), _decode(payload.get("ciphertext"))
            if len(nonce) != _NONCE_BYTES:
                raise ValueError
            metadata = {
                key: value for key, value in payload.items() if key not in {"nonce", "ciphertext"}
            }
            return (
                AESGCM(self._key)
                .decrypt(nonce, ciphertext, self._aad(reference, metadata))
                .decode("utf-8")
            )
        except SecretPermissionError:
            raise
        except (InvalidTag, UnicodeDecodeError, OSError, TypeError, ValueError):
            raise SecretUnavailableError("stored secret is unavailable") from None

    def _aad(self, reference: str, metadata: Mapping[str, object]) -> bytes:
        if self._scope is None:
            return reference.encode("ascii")
        return json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def delete(self, reference: str) -> None:
        secret_id = self._parse_secret_reference(reference)
        with self._locked(secret_id):
            path = self._path_for(secret_id)
            if not path.exists():
                return
            self._resolve_file(reference, secret_id)
            try:
                path.unlink()
                _sync_directory(self.secret_dir)
            except OSError:
                raise SecretUnavailableError("stored secret could not be removed") from None

    @contextmanager
    def _locked(self, secret_id: UUID) -> Iterator[None]:
        # The lock namespace deliberately excludes scope: ownership must not
        # change between authentication and unlink in another process.
        _ensure_private_directory(self.secret_dir)
        lock_dir = self.secret_dir / ".locks"
        _ensure_private_directory(lock_dir)
        with (lock_dir / f"{secret_id}.lock").open("a+b") as handle:
            if os.name == "nt":
                import msvcrt

                windows_lock = cast(_WindowsFileLock, msvcrt)
                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                windows_lock.locking(handle.fileno(), windows_lock.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    windows_lock.locking(handle.fileno(), windows_lock.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _load_or_create_key(self) -> bytes:
        try:
            if self.master_key_path.exists():
                _require_owner_only(self.master_key_path)
                key = self.master_key_path.read_bytes()
            else:
                if not self._create_key:
                    raise SecretKeyError("master key is unavailable")
                self.master_key_path.parent.mkdir(parents=True, exist_ok=True)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_BINARY"):
                    flags |= os.O_BINARY
                descriptor = os.open(self.master_key_path, flags, 0o600)
                try:
                    key = random_secrets.token_bytes(_KEY_BYTES)
                    os.write(descriptor, key)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except SecretPermissionError:
            raise
        except OSError:
            raise SecretKeyError("master key is unavailable") from None
        if len(key) != _KEY_BYTES:
            raise SecretKeyError("master key is invalid")
        return key

    def _path_for(self, secret_id: UUID) -> Path:
        return self.secret_dir / f"{secret_id}.json"

    @staticmethod
    def _parse_secret_reference(reference: str) -> UUID:
        match = _SECRET_REFERENCE.fullmatch(reference)
        if match is None:
            raise SecretReferenceError("secret reference is invalid")
        try:
            return UUID(match.group(1))
        except ValueError:
            raise SecretReferenceError("secret reference is invalid") from None

    @staticmethod
    def _atomic_write(path: Path, payload: Mapping[str, object]) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                if hasattr(os, "chmod"):
                    os.chmod(temporary.name, stat.S_IRUSR | stat.S_IWUSR)
                json.dump(payload, temporary, sort_keys=True, separators=(",", ":"))
                temporary.flush()
                os.fsync(temporary.fileno())
            # A predeclared adoption UUID may already be committed by a retry.
            # Publish without replacing it, even across processes.
            os.link(temporary_path, path)
            _sync_directory(path.parent)
            if hasattr(os, "chmod"):
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            raise SecretStoreError("encrypted secret could not be stored") from None
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return  # Windows does not expose directory fsync through os.open.
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_owner_only(path: Path) -> None:
    # Native Windows ACLs are not represented by POSIX mode bits. WSL's
    # DrvFs/OneDrive mounts expose the same limitation; Windows owns the ACL
    # there, while ordinary Linux paths remain fail-closed on group/world bits.
    if os.name == "nt" or _is_windows_managed_path(path):
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        raise SecretPermissionError("secret file permissions are unavailable") from None
    if mode & 0o077:
        raise SecretPermissionError("secret file permissions must be owner-only")


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        if os.name != "nt" and not _is_windows_managed_path(path):
            os.chmod(path, stat.S_IRWXU)
        _require_owner_only(path)
    except SecretPermissionError:
        raise
    except OSError:
        raise SecretPermissionError("secret directory is unavailable") from None


def _is_windows_managed_path(path: Path) -> bool:
    return str(path.resolve()).casefold().startswith("/mnt/")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _decode(value: object) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError
    return base64.urlsafe_b64decode(value.encode("ascii"))


__all__ = [
    "FileSecretStore",
    "SecretKeyError",
    "SecretPermissionError",
    "SecretReferenceError",
    "SecretResolverPort",
    "SecretStoreError",
    "SecretStorePort",
    "SecretUnavailableError",
    "TemporarySecretStorePort",
]
