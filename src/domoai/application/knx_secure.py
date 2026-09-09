"""Compatibility exports for the KNX Secure secret service."""

from domoai.security.knx_secure import (
    KnxSecureCredentials,
    KnxSecureCredentialStore,
    KnxSecureError,
    knx_secure_diagnostic,
)

__all__ = [
    "KnxSecureCredentialStore",
    "KnxSecureCredentials",
    "KnxSecureError",
    "knx_secure_diagnostic",
]
