"""Read-only conformance checks for the Connection Provider SDK."""

from __future__ import annotations

from dataclasses import dataclass

from domoai.runtime.connection_provider_loader import ConnectionProviderLoader


@dataclass(frozen=True)
class ProviderConformanceDiagnostic:
    code: str
    message: str


def validate_provider(provider: object) -> tuple[ProviderConformanceDiagnostic, ...]:
    """Validate a provider instance without invoking network or adapter code."""

    try:
        ConnectionProviderLoader._validate(provider)
    except ValueError as error:
        code = str(error)
        known_codes = {
            "provider_contract_unsupported",
            "provider_entry_point_invalid",
            "provider_metadata_sensitive",
        }
        normalized = code if code in known_codes else "provider_entry_point_invalid"
        return (
            ProviderConformanceDiagnostic(
                code=normalized,
                message="connection provider does not conform to the v1 SDK",
            ),
        )
    return ()


__all__ = ["ProviderConformanceDiagnostic", "validate_provider"]
