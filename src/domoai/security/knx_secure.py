"""Secret-backed KNX Secure credential handling."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from domoai.domain.connections import ConnectionDiagnostic, DiagnosticSeverity
from domoai.security.secrets import SecretResolverPort, SecretStorePort

_SCHEMA_VERSION = "v1"
_FIELDS = frozenset(
    {
        "keyring_password",
        "user_password",
        "device_authentication_password",
        "backbone_key",
    }
)


class KnxSecureError(RuntimeError):
    """Sanitized KNX Secure credential error."""


@dataclass(frozen=True, repr=False)
class KnxSecureCredentials:
    """Resolved credentials, held only while constructing a live transport."""

    keyring_password: str | None = None
    user_password: str | None = None
    device_authentication_password: str | None = None
    backbone_key: str | None = None

    def __repr__(self) -> str:
        return (
            "KnxSecureCredentials("
            f"has_keyring_password={self.keyring_password is not None}, "
            f"has_user_password={self.user_password is not None}, "
            "has_device_authentication_password="
            f"{self.device_authentication_password is not None}, "
            f"has_backbone_key={self.backbone_key is not None})"
        )


class KnxSecureCredentialStore:
    """Store, resolve and rotate a versioned KNX Secure bundle."""

    def __init__(
        self, store: SecretStorePort | SecretResolverPort, *, audit: Any | None = None
    ) -> None:
        self.store = store
        self.audit = audit

    def put(
        self,
        *,
        keyring_password: str | None = None,
        user_password: str | None = None,
        device_authentication_password: str | None = None,
        backbone_key: str | None = None,
    ) -> str:
        credentials = self._credentials(
            keyring_password=keyring_password,
            user_password=user_password,
            device_authentication_password=device_authentication_password,
            backbone_key=backbone_key,
        )
        put = getattr(self.store, "put", None)
        if not callable(put):
            raise KnxSecureError("KNX Secure credential storage is unavailable")
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "keyring_password": credentials.keyring_password,
            "user_password": credentials.user_password,
            "device_authentication_password": credentials.device_authentication_password,
            "backbone_key": credentials.backbone_key,
        }
        try:
            return str(put(json.dumps(payload, sort_keys=True, separators=(",", ":"))))
        except Exception as error:  # noqa: BLE001 - secret-store boundary is sanitized
            raise KnxSecureError("KNX Secure credentials could not be stored") from error

    def resolve(self, reference: str) -> KnxSecureCredentials:
        try:
            payload = json.loads(self.store.resolve(reference))
            if not isinstance(payload, dict):
                raise ValueError
            schema_version = payload.get("schema_version")
            if schema_version not in {None, _SCHEMA_VERSION}:
                raise ValueError
            expected_keys = _FIELDS | ({"schema_version"} if schema_version else set())
            if set(payload) != expected_keys:
                raise ValueError
            if any(
                value is not None and (not isinstance(value, str) or not value)
                for key, value in payload.items()
                if key != "schema_version"
            ):
                raise ValueError
            return self._credentials(
                keyring_password=payload["keyring_password"],
                user_password=payload["user_password"],
                device_authentication_password=payload["device_authentication_password"],
                backbone_key=payload["backbone_key"],
            )
        except KnxSecureError:
            raise
        except Exception as error:  # noqa: BLE001 - never expose provider secret errors
            raise KnxSecureError("KNX Secure credentials are invalid") from error

    def rotate(self, old_reference: str, **credentials: str | None) -> str:
        """Validate old material, then store new material without deleting old."""

        self.resolve(old_reference)
        if any(key not in _FIELDS for key in credentials):
            raise KnxSecureError("KNX Secure credential fields are invalid")
        new_reference = self.put(**credentials)
        if self.audit is not None:
            self.audit.append(
                event_type="knx_secure_credentials_rotated",
                actor="knx_secure",
                subject_id=new_reference,
                payload={
                    "old_credential_ref": old_reference,
                    "new_credential_ref": new_reference,
                },
            )
        return new_reference

    @staticmethod
    def _credentials(**values: str | None) -> KnxSecureCredentials:
        if not any(value is not None for value in values.values()):
            raise KnxSecureError("KNX Secure credentials are empty")
        return KnxSecureCredentials(**values)


def knx_secure_diagnostic(_error: Exception) -> ConnectionDiagnostic:
    """Return a stable diagnostic without copying exception text."""

    return ConnectionDiagnostic(
        code="knx_secure_configuration_invalid",
        severity=DiagnosticSeverity.ERROR,
        retryable=False,
        user_action="Update KNX Secure credentials",
        message="KNX Secure configuration is invalid",
    )


__all__ = [
    "KnxSecureCredentialStore",
    "KnxSecureCredentials",
    "KnxSecureError",
    "knx_secure_diagnostic",
]
