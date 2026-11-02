from __future__ import annotations

import pytest
from pydantic import ValidationError

from domoai.domain.connections import ConnectionCandidate, ConnectionDraft
from domoai.runtime.connection_ports import ConnectionProviderManifest, ConnectionSetup
from domoai.runtime.connection_provider_base import ConnectionProviderBase


class _Provider(ConnectionProviderBase):
    manifest = ConnectionProviderManifest(
        connector_id="base_fixture",
        display_name="Base Fixture",
        protocol="fixture",
        version="1.0",
    )


def test_base_provider_builds_stable_manual_candidate() -> None:
    first = _Provider().manual_candidate({"url": "https://fixture.example"})
    second = _Provider().manual_candidate({"url": "https://fixture.example"})

    assert first == second
    assert first.connector_id == "base_fixture"
    assert first.metadata["source"] == "manual"


def test_base_provider_rejects_credential_bearing_manual_endpoint() -> None:
    with pytest.raises(ValidationError):
        _Provider().manual_candidate({"url": "https://user:password@example"})


def test_base_provider_rejects_malformed_url_endpoint() -> None:
    with pytest.raises(ValueError, match="endpoint"):
        _Provider().manual_candidate({"url": "not-a-url"})


@pytest.mark.asyncio
async def test_base_provider_reports_incomplete_without_claiming_validation() -> None:
    candidate = ConnectionCandidate(
        connector_id="base_fixture",
        candidate_id="manual",
        display_name="Base Fixture",
        endpoint={"url": "https://fixture.example"},
    )
    setup = ConnectionSetup(
        candidate=candidate,
        draft=ConnectionDraft(
            connector_id="base_fixture",
            unique_id="manual",
            endpoint=candidate.endpoint,
            options={},
        ),
    )

    result = await _Provider().validate(setup)

    assert result.valid is False
    assert result.diagnostics[0].code == "provider_adapter_not_implemented"
    assert "credential" not in result.diagnostics[0].message.casefold()
