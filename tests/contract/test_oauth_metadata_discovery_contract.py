from __future__ import annotations

import pytest
from pydantic import ValidationError

from domoai.domain.p2_connections import (
    OAuthMetadataDiscoveryResult,
    OAuthMetadataSource,
    OAuthProviderMetadata,
)


def test_oauth_metadata_is_secret_free_and_versioned() -> None:
    metadata = OAuthProviderMetadata(
        issuer="https://issuer.example",
        authorization_url="https://issuer.example/authorize",
        token_url="https://issuer.example/token",
        revoke_url=None,
        scopes=("read", "config"),
        source=OAuthMetadataSource.OPENID_CONFIGURATION,
    )
    result = OAuthMetadataDiscoveryResult(metadata=metadata, manual_required=False)

    assert result.metadata is not None
    assert result.metadata.source is OAuthMetadataSource.OPENID_CONFIGURATION
    assert "access_token" not in result.model_dump_json()
    assert "client_secret" not in result.model_dump_json()


def test_oauth_metadata_rejects_embedded_credentials_and_duplicate_scopes() -> None:
    with pytest.raises(ValidationError, match="credentials"):
        OAuthProviderMetadata(
            issuer="https://issuer.example",
            authorization_url="https://user:password@issuer.example/authorize",
            token_url="https://issuer.example/token",
            scopes=("read",),
            source=OAuthMetadataSource.MANUAL,
        )

    with pytest.raises(ValidationError, match="scopes"):
        OAuthProviderMetadata(
            issuer=None,
            authorization_url="https://issuer.example/authorize",
            token_url="https://issuer.example/token",
            scopes=("read", "read"),
            source=OAuthMetadataSource.MANUAL,
        )


def test_manual_fallback_has_a_diagnostic_and_no_metadata() -> None:
    result = OAuthMetadataDiscoveryResult(
        metadata=None,
        manual_required=True,
        diagnostic_code="oauth_metadata_unavailable",
        diagnostic_message="OAuth metadata is unavailable; enter the provider fields manually",
    )

    assert result.manual_required is True
    assert result.metadata is None
    assert result.diagnostic_code == "oauth_metadata_unavailable"


def test_oauth_discovery_schemas_are_published() -> None:
    import json
    from pathlib import Path

    root = Path(__file__).parents[2] / "schemas" / "v1"
    metadata = json.loads(
        (root / "oauth-provider-metadata.schema.json").read_text(encoding="utf-8")
    )
    result = json.loads(
        (root / "oauth-metadata-discovery-result.schema.json").read_text(encoding="utf-8")
    )

    assert metadata["properties"]["schema_version"]["const"] == "v1"
    assert "manual_required" in result["properties"]
