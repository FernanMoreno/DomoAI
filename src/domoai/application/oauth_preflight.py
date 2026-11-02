"""Offline readiness checks for the Home Assistant OAuth deployment boundary."""

from __future__ import annotations

import hashlib
import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import Field

from domoai.adapters.home_assistant.oauth import HomeAssistantOAuthConfig
from domoai.domain.models import StrictModel
from domoai.security.oauth_urls import validate_oauth_url

OAuthPreflightStatus = Literal["ready", "manual_required", "invalid"]

_ENV_TO_FIELD = {
    "DOMOAI_HOME_ASSISTANT_OAUTH_AUTHORIZATION_URL": "authorization_url",
    "DOMOAI_HOME_ASSISTANT_OAUTH_TOKEN_URL": "token_url",
    "DOMOAI_HOME_ASSISTANT_OAUTH_REVOKE_URL": "revoke_url",
    "DOMOAI_HOME_ASSISTANT_OAUTH_CLIENT_ID": "client_id",
    "DOMOAI_HOME_ASSISTANT_OAUTH_REDIRECT_URI": "redirect_uri",
    "DOMOAI_HOME_ASSISTANT_OAUTH_SCOPES": "scopes",
}
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REQUIRED_FIELDS = (
    "authorization_url",
    "token_url",
    "client_id",
    "redirect_uri",
    "scopes",
)


class OAuthPreflightDiagnostic(StrictModel):
    code: str = Field(min_length=1, max_length=96)
    message: str = Field(min_length=1, max_length=256)


class OAuthDeploymentPreflight(StrictModel):
    """Secret-free result of local OAuth configuration validation."""

    status: OAuthPreflightStatus
    missing_fields: tuple[str, ...] = ()
    diagnostics: tuple[OAuthPreflightDiagnostic, ...] = ()
    external_registration_required: bool = True
    configuration_fingerprint: str | None = None

    @classmethod
    def from_environment(cls, values: Mapping[str, str]) -> OAuthDeploymentPreflight:
        normalized = {
            str(key): value.strip()
            for key, value in values.items()
            if isinstance(value, str)
        }
        forbidden = _forbidden_oauth_keys(normalized)
        if forbidden:
            return cls(
                status="invalid",
                diagnostics=(
                    OAuthPreflightDiagnostic(
                        code="oauth_secret_field_forbidden",
                        message="OAuth client secrets and tokens belong in the secret store",
                    ),
                ),
            )

        selected = {
            field: normalized.get(env_name, "")
            for env_name, field in _ENV_TO_FIELD.items()
        }
        try:
            for field in ("authorization_url", "token_url", "revoke_url", "redirect_uri"):
                if selected[field]:
                    _validate_oauth_url(selected[field], field)
            if selected["scopes"]:
                _scopes(selected["scopes"])
            if selected["redirect_uri"]:
                _validate_redirect(selected["redirect_uri"])
        except ValueError:
            return cls(
                status="invalid",
                diagnostics=(
                    OAuthPreflightDiagnostic(
                        code="oauth_configuration_invalid",
                        message="OAuth URLs, redirect URI or scopes are invalid",
                    ),
                ),
            )
        missing = tuple(field for field in _REQUIRED_FIELDS if not selected[field])
        diagnostics: list[OAuthPreflightDiagnostic] = []
        if missing:
            diagnostics.append(
                OAuthPreflightDiagnostic(
                    code="oauth_configuration_incomplete",
                    message="Required OAuth configuration fields are missing",
                )
            )
            diagnostics.append(
                OAuthPreflightDiagnostic(
                    code="oauth_client_registration_required",
                    message="Register the OAuth client in the external provider",
                )
            )
            return cls(
                status="manual_required",
                missing_fields=missing,
                diagnostics=tuple(diagnostics),
            )

        try:
            scopes = _scopes(selected["scopes"])
            config = HomeAssistantOAuthConfig(
                authorization_url=selected["authorization_url"],
                token_url=selected["token_url"],
                revoke_url=selected["revoke_url"] or None,
                client_id=selected["client_id"],
                redirect_uri=selected["redirect_uri"],
                scopes=scopes,
            )
            _validate_redirect(config.redirect_uri)
        except ValueError:
            return cls(
                status="invalid",
                diagnostics=(
                    OAuthPreflightDiagnostic(
                        code="oauth_configuration_invalid",
                        message="OAuth URLs, redirect URI or scopes are invalid",
                    ),
                ),
            )

        if config.revoke_url is None:
            diagnostics.append(
                OAuthPreflightDiagnostic(
                    code="oauth_revoke_endpoint_not_configured",
                    message="Configure revocation before enabling token revocation",
                )
            )
        diagnostics.append(
            OAuthPreflightDiagnostic(
                code="oauth_client_registration_required",
                message="Register the OAuth client and redirect URI in the external provider",
            )
        )
        fingerprint = _fingerprint(config, scopes)
        return cls(
            status="manual_required",
            diagnostics=tuple(diagnostics),
            configuration_fingerprint=fingerprint,
        )

    @classmethod
    def from_settings(cls, settings: object) -> OAuthDeploymentPreflight:
        """Validate an already parsed Settings object without reading secrets."""

        return cls.from_environment(
            {
                "DOMOAI_HOME_ASSISTANT_OAUTH_AUTHORIZATION_URL": str(
                    getattr(settings, "home_assistant_oauth_authorization_url", "") or ""
                ),
                "DOMOAI_HOME_ASSISTANT_OAUTH_TOKEN_URL": str(
                    getattr(settings, "home_assistant_oauth_token_url", "") or ""
                ),
                "DOMOAI_HOME_ASSISTANT_OAUTH_REVOKE_URL": str(
                    getattr(settings, "home_assistant_oauth_revoke_url", "") or ""
                ),
                "DOMOAI_HOME_ASSISTANT_OAUTH_CLIENT_ID": str(
                    getattr(settings, "home_assistant_oauth_client_id", "") or ""
                ),
                "DOMOAI_HOME_ASSISTANT_OAUTH_REDIRECT_URI": str(
                    getattr(settings, "home_assistant_oauth_redirect_uri", "") or ""
                ),
                "DOMOAI_HOME_ASSISTANT_OAUTH_SCOPES": ",".join(
                    str(item)
                    for item in getattr(settings, "home_assistant_oauth_scopes", ())
                ),
            }
        )

    @classmethod
    def from_env_file(cls, path: Path) -> OAuthDeploymentPreflight:
        try:
            values = _parse_env_file(path)
        except (OSError, UnicodeError, ValueError):
            return cls(
                status="invalid",
                diagnostics=(
                    OAuthPreflightDiagnostic(
                        code="oauth_env_file_invalid",
                        message="OAuth environment file could not be read safely",
                    ),
                ),
            )
        return cls.from_environment(values)


def _forbidden_oauth_keys(values: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(
        key
        for key in values
        if key.upper().endswith(("_CLIENT_SECRET", "_ACCESS_TOKEN", "_REFRESH_TOKEN"))
    )


def _scopes(raw: str) -> tuple[str, ...]:
    values = tuple(item for item in re.split(r"[\s,]+", raw.strip()) if item)
    if not values or len(values) != len(set(values)):
        raise ValueError("OAuth scopes must be unique and non-empty")
    return values


def _validate_redirect(value: str) -> None:
    validate_oauth_url(value, "redirect_uri")


def _validate_oauth_url(value: str, field_name: str) -> None:
    validate_oauth_url(value, field_name)


def _fingerprint(config: HomeAssistantOAuthConfig, scopes: tuple[str, ...]) -> str:
    payload = "\x1f".join(
        (
            config.authorization_url,
            config.token_url,
            config.revoke_url or "",
            config.client_id,
            config.redirect_uri,
            *scopes,
        )
    )
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _parse_env_file(path: Path) -> dict[str, str]:
    content = path.read_text(encoding="utf-8")
    values: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError("invalid environment line")
        key, raw_value = line.split("=", 1)
        if _ENV_KEY.fullmatch(key) is None:
            raise ValueError("invalid environment key")
        if key in values:
            raise ValueError("duplicate environment key")
        parsed = shlex.split(raw_value, comments=True, posix=True)
        if len(parsed) > 1:
            raise ValueError("environment value contains multiple tokens")
        values[key] = parsed[0] if parsed else ""
    return values


__all__ = ["OAuthDeploymentPreflight", "OAuthPreflightDiagnostic"]
