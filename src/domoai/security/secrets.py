"""Encrypted local secret storage and reference-only resolution.

The control plane deliberately keeps this module outside domain and runtime
models. Callers receive a reference or a resolved value at the narrowest
possible boundary; persisted connection profiles never receive the value.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets as random_secrets
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

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


class FileSecretStore(SecretStorePort):
    """Owner-readable AES-GCM secret store backed by local files."""

    key_version = _PAYLOAD_VERSION

    def __init__(self, master_key_path: Path, *, secret_dir: Path | None = None) -> None:
        self.master_key_path = Path(master_key_path)
        self.secret_dir = secret_dir or self.master_key_path.parent / "secrets"
        self._key = self._load_or_create_key()

    def __repr__(self) -> str:
        return (
            f"FileSecretStore(master_key_path={str(self.master_key_path)!r}, "
            f"secret_dir={str(self.secret_dir)!r})"
        )

    def put(self, value: str) -> str:
        if not isinstance(value, str) or not value:
            raise SecretStoreError("secret value must be a non-empty string")
        reference_id = uuid4()
        reference = f"secret:{reference_id}"
        nonce = random_secrets.token_bytes(_NONCE_BYTES)
        ciphertext = AESGCM(self._key).encrypt(
            nonce,
            value.encode("utf-8"),
            reference.encode("ascii"),
        )
        payload = {
            "version": _PAYLOAD_VERSION,
            "key_version": self.key_version,
            "reference": str(reference_id),
            "nonce": _encode(nonce),
            "ciphertext": _encode(ciphertext),
        }
        _ensure_private_directory(self.secret_dir)
        self._atomic_write(self._path_for(reference_id), payload)
        return reference

    def resolve(self, reference: str) -> str:
        if not isinstance(reference, str):
            raise SecretReferenceError("secret reference is invalid")
        env_match = _ENV_REFERENCE.fullmatch(reference)
        if env_match is not None:
            value = os.getenv(env_match.group(1))
            if value is None:
                raise SecretUnavailableError("environment secret is unavailable")
            return value
        secret_id = self._parse_secret_reference(reference)
        path = self._path_for(secret_id)
        if not path.exists():
            raise SecretUnavailableError("stored secret is unavailable")
        try:
            _require_owner_only(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                raise ValueError
            if payload.get("version") != _PAYLOAD_VERSION:
                raise ValueError
            if payload.get("key_version", _PAYLOAD_VERSION) != self.key_version:
                raise ValueError
            if payload.get("reference") != str(secret_id):
                raise ValueError
            nonce = _decode(payload.get("nonce"))
            ciphertext = _decode(payload.get("ciphertext"))
            if len(nonce) != _NONCE_BYTES:
                raise ValueError
            plaintext = AESGCM(self._key).decrypt(
                nonce,
                ciphertext,
                reference.encode("ascii"),
            )
            return plaintext.decode("utf-8")
        except SecretStoreError:
            raise
        except (
            InvalidTag,
            UnicodeDecodeError,
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            raise SecretUnavailableError("stored secret is unavailable") from None

    def delete(self, reference: str) -> None:
        secret_id = self._parse_secret_reference(reference)
        path = self._path_for(secret_id)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            raise SecretUnavailableError("stored secret could not be removed") from None

    def _load_or_create_key(self) -> bytes:
        try:
            if self.master_key_path.exists():
                _require_owner_only(self.master_key_path)
                key = self.master_key_path.read_bytes()
            else:
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
            os.replace(temporary_path, path)
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
]
