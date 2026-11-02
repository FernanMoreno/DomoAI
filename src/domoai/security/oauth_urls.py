"""Shared transport policy for OAuth endpoint URLs."""

from __future__ import annotations

from urllib.parse import ParseResult, urlparse

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def validate_oauth_url(value: str, field_name: str) -> ParseResult:
    """Validate an OAuth URL without resolving or contacting its host."""

    try:
        parsed = urlparse(value.strip())
        hostname = parsed.hostname
    except ValueError as error:
        raise ValueError(f"OAuth {field_name} is invalid") from error
    if (
        parsed.scheme not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(f"OAuth {field_name} is invalid")
    if parsed.scheme == "http" and hostname.casefold() not in _LOOPBACK_HOSTS:
        raise ValueError(f"OAuth {field_name} must use HTTPS unless it is loopback")
    return parsed


__all__ = ["validate_oauth_url"]
