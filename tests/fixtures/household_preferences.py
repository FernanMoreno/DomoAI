"""Deterministic P02 inputs; PostgreSQL uses the isolated phase3 container recipe."""

from datetime import UTC, datetime
from uuid import UUID

from domoai.domain.models import (
    AuthorityContext,
    Capability,
    CapabilityKind,
    Device,
    DeviceType,
    PrincipalRole,
    SourceRef,
)
from domoai.runtime.clock import FixedClock

NOW = datetime(2026, 9, 10, 20, tzinfo=UTC)
GENERATION = UUID("00000000-0000-4000-8000-000000000001")


def clock():
    return FixedClock(NOW)


def authority(principal="resident-1", role=PrincipalRole.OWNER, household="home-a", **scope):
    return AuthorityContext(
        tenant_id="tenant-a",
        household_id=household,
        household_ids=[household],
        principal_id=principal,
        roles=[role],
        **scope,
    )


def preference(**changes):
    payload = {
        "key": "energy.device_use_allowed",
        "target": {"kind": "device", "id": "water-heater"},
        "value": False,
        "unit": "dimensionless",
        "valid_from": "2026-09-10T22:00:00+02:00",
        "valid_until": "2026-09-11T07:00:00+02:00",
        "timezone": "Europe/Madrid",
        "provenance": {"source_kind": "user_request", "source_ref": "request-1"},
    }
    return payload | changes


def mutation(version=None, **changes):
    return {
        "expected_version": version or {"generation": str(GENERATION), "revision": 0},
        "idempotency_key": "request-1",
        "preference": preference(),
    } | changes


def devices():
    return [
        Device(
            id=identity,
            name="Water heater",
            type=DeviceType.SWITCH,
            protocol="fixture",
            source_refs=[SourceRef(adapter_id="fixture", external_id=identity)],
            capabilities=[
                Capability(
                    name="switch.on",
                    kind=CapabilityKind.BOOLEAN,
                    readable=True,
                    writable=True,
                    commands=["switch.on", "switch.off"],
                )
            ],
        )
        for identity in ("water-heater", "water-heater-two")
    ]
