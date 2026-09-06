from __future__ import annotations

import pytest

from domoai.lab.qualification import (
    REQUIRED_ADAPTERS,
    REQUIRED_CHECKS,
    REQUIRED_DOMAINS,
    DigitalTwinQualificationRunner,
)


def test_matrix_declares_every_adapter_domain_and_cross_cutting_check() -> None:
    assert REQUIRED_ADAPTERS == {
        "fixture",
        "home_assistant",
        "knx",
        "modbus",
        "matter",
        "zigbee2mqtt",
    }
    assert REQUIRED_DOMAINS == {
        "light",
        "switch",
        "cover",
        "climate",
        "environment",
        "power",
        "battery",
        "ev",
        "water",
        "solar",
    }
    assert {
        "discovery",
        "normalization",
        "routing",
        "readback",
        "faults",
        "idempotency",
        "audit",
        "recovery",
        "scheduler",
        "automation",
        "optimization",
        "product",
        "privacy",
        "identity",
    } <= REQUIRED_CHECKS


@pytest.mark.asyncio
async def test_complete_matrix_passes_and_is_reproducible() -> None:
    first = await DigitalTwinQualificationRunner(seed=187).run()
    second = await DigitalTwinQualificationRunner(seed=187).run()

    assert first.status == "passed"
    assert first.coverage.adapters == sorted(REQUIRED_ADAPTERS)
    assert first.coverage.domains == sorted(REQUIRED_DOMAINS)
    assert first.invariant_violations == []
    assert first.trace_digest == second.trace_digest
    assert first.canonical_json() == second.canonical_json()

