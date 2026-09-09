"""Management bind and authentication policy."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress


def is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ManagementAuth:
    def __init__(
        self,
        host: str,
        *,
        token: str | None,
        tls_enabled: bool,
        external_auth: bool = False,
    ) -> None:
        if token is not None and not token:
            raise ValueError("management authentication token must be non-empty")
        if not is_loopback_host(host) and token is None and not external_auth:
            raise ValueError("non-loopback management requires authentication")
        if not is_loopback_host(host) and not tls_enabled:
            raise ValueError("non-loopback management requires HTTPS")
        self.host = host
        self._token_digest = (
            hashlib.sha256(token.encode("utf-8")).digest() if token is not None else None
        )

    def authorized(self, header: str | None) -> bool:
        if self._token_digest is None:
            return True
        if not header or not header.casefold().startswith("bearer "):
            return False
        token = header[7:].strip()
        if not token:
            return False
        return hmac.compare_digest(
            hashlib.sha256(token.encode("utf-8")).digest(), self._token_digest
        )


__all__ = ["ManagementAuth", "is_loopback_host"]
