from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from domoai.domain.commissioning import (
    CommissioningAssetType,
    CommissioningCandidate,
    CommissioningCandidateStatus,
    CommissioningReport,
    CommissioningRoute,
)
from domoai.domain.models import DeviceType, SourceRef


def route(*, writable: bool = False) -> CommissioningRoute:
    return CommissioningRoute(
        provider_id="fixture",
        capability="battery.soc",
        source_ref=SourceRef(
            adapter_id="fixture",
            external_id="sensor.battery_soc",
            source_device_id="battery-1",
        ),
        source_device_id="battery-1",
        commands=["charge"] if writable else [],
        readable=True,
        writable=writable,
        available=True,
    )


def candidate() -> CommissioningCandidate:
    return CommissioningCandidate(
        asset_type=CommissioningAssetType.BATTERY,
        canonical_device_id="garage.battery",
        name="Battery",
        device_type=DeviceType.ENERGY,
        provider_ids=["fixture"],
        source_refs=[route().source_ref],
        identity_keys=["fixture:battery-1"],
        connections=["fixture:bus:1"],
        required_capabilities=["battery.soc"],
        routes=[route()],
        status=CommissioningCandidateStatus.READY_FOR_BINDING,
        blockers=[],
        next_actions=["provide_server_owned_binding"],
        candidate_digest="a" * 64,
    )


def test_commissioning_report_is_versioned_and_explicitly_non_authoritative() -> None:
    report = CommissioningReport(
        runtime_revision="rev-1",
        generated_at=datetime(2026, 8, 31, tzinfo=UTC),
        candidates=[candidate()],
        report_digest="b" * 64,
    )

    assert report.schema_version == "v1"
    assert report.authority_created is False
    assert report.model_dump(mode="json")["authority_created"] is False


def test_route_rejects_unknown_provider_metadata_instead_of_persisting_secrets() -> None:
    with pytest.raises(ValidationError):
        CommissioningRoute.model_validate(
            {**route().model_dump(), "password": "do-not-persist"}
        )


def test_candidate_rejects_non_sha256_digest() -> None:
    with pytest.raises(ValidationError):
        CommissioningCandidate.model_validate(
            {**candidate().model_dump(), "candidate_digest": "not-a-digest"}
        )
