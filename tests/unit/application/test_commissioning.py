import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from domoai.application.commissioning import (
    CommissioningPersistenceError,
    CommissioningService,
)
from domoai.domain.models import AdapterSnapshot
from domoai.runtime.clock import FixedClock
from domoai.runtime.registry import DeviceRegistry


def capability(name: str, *, writable: bool = False, commands: list[str] | None = None) -> dict:
    return {
        "name": name,
        "kind": "number",
        "unit": "kW" if name in {"battery.power", "ev_charging"} else "kWh",
        "readable": True,
        "writable": writable,
        "commands": commands or [],
    }


def entity(
    *,
    entity_id: str,
    canonical_id: str,
    semantic_type: str,
    capabilities: list[dict],
    available: bool = True,
) -> dict:
    return {
        "entity_id": entity_id,
        "source_device_id": canonical_id,
        "canonical_id": canonical_id,
        "identity_keys": [f"fixture:{canonical_id}"],
        "connections": [f"fixture:bus:{canonical_id}"],
        "name": canonical_id.replace(".", " ").title(),
        "domain": semantic_type,
        "semantic_type": semantic_type,
        "capabilities": capabilities,
        "available": available,
    }


def battery_capabilities(*, control: bool = True) -> list[dict]:
    values = [
        capability("battery.soc"),
        capability("battery.power"),
        capability("battery.capacity"),
    ]
    if control:
        values.append(
            capability(
                "battery_control",
                writable=True,
                commands=["charge", "discharge", "stop"],
            )
        )
    return values


def build_registry(*entities: dict) -> DeviceRegistry:
    registry = DeviceRegistry()
    for item in entities:
        registry.apply_snapshot(
            AdapterSnapshot(source_entities=[item]),
            str(item["entity_id"]).split(".", 1)[0],
        )
    return registry


def service(registry: DeviceRegistry, path: Path) -> CommissioningService:
    return CommissioningService(
        registry,
        clock=FixedClock(datetime(2026, 8, 31, tzinfo=UTC)),
        manifest_path=path,
    )


def test_complete_battery_is_ready_for_explicit_binding_but_not_authorized(tmp_path: Path) -> None:
    registry = build_registry(
        entity(
            entity_id="fixture.battery",
            canonical_id="garage.battery",
            semantic_type="energy",
            capabilities=battery_capabilities(),
        )
    )

    report = service(registry, tmp_path / "commissioning.json").inspect(
        runtime_revision="rev-1"
    )

    candidate = report.candidates[0]
    assert candidate.status.value == "ready_for_binding"
    assert report.authority_created is False
    assert candidate.next_actions == ["provide_server_owned_binding"]
    assert (tmp_path / "commissioning.json").is_file()


def test_telemetry_only_battery_is_observed_only(tmp_path: Path) -> None:
    registry = build_registry(
        entity(
            entity_id="fixture.battery",
            canonical_id="garage.battery",
            semantic_type="energy",
            capabilities=battery_capabilities(control=False),
        )
    )

    candidate = service(registry, tmp_path / "commissioning.json").inspect(
        runtime_revision="rev-1"
    ).candidates[0]

    assert candidate.status.value == "observed_only"
    assert "missing_control_route" in {item.code for item in candidate.blockers}


def test_ambiguous_source_route_is_blocked_and_never_selected(tmp_path: Path) -> None:
    first = entity(
        entity_id="fixture_a.battery",
        canonical_id="garage.battery",
        semantic_type="energy",
        capabilities=battery_capabilities(),
    )
    second = entity(
        entity_id="fixture_b.battery",
        canonical_id="garage.battery",
        semantic_type="energy",
        capabilities=battery_capabilities(),
    )
    registry = build_registry(first, second)

    candidate = service(registry, tmp_path / "commissioning.json").inspect(
        runtime_revision="rev-1"
    ).candidates[0]

    assert candidate.status.value == "blocked"
    assert "ambiguous_route" in {item.code for item in candidate.blockers}
    assert len(candidate.routes) >= 2


def test_unavailable_source_is_blocked(tmp_path: Path) -> None:
    registry = build_registry(
        entity(
            entity_id="fixture.battery",
            canonical_id="garage.battery",
            semantic_type="energy",
            capabilities=battery_capabilities(),
            available=False,
        )
    )

    candidate = service(registry, tmp_path / "commissioning.json").inspect(
        runtime_revision="rev-1"
    ).candidates[0]

    assert candidate.status.value == "blocked"
    assert "source_unavailable" in {item.code for item in candidate.blockers}


def test_report_replacement_is_secret_free_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "commissioning.json"
    registry = build_registry(
        entity(
            entity_id="fixture.battery",
            canonical_id="garage.battery",
            semantic_type="energy",
            capabilities=battery_capabilities(),
        )
    )
    commissioning = service(registry, path)

    first = commissioning.inspect(runtime_revision="rev-1")
    first_payload = path.read_text(encoding="utf-8")
    second = commissioning.inspect(runtime_revision="rev-1")
    second_payload = path.read_text(encoding="utf-8")

    assert first.report_digest == second.report_digest
    assert first_payload == second_payload
    assert json.loads(second_payload)["authority_created"] is False
    assert all(
        secret not in second_payload.casefold()
        for secret in ("password", "token", "secret")
    )


def test_report_persistence_failure_is_bounded_and_does_not_return_partial_json(
    tmp_path: Path,
) -> None:
    path = tmp_path / "commissioning-directory"
    path.mkdir()
    registry = build_registry(
        entity(
            entity_id="fixture.battery",
            canonical_id="garage.battery",
            semantic_type="energy",
            capabilities=battery_capabilities(),
        )
    )

    with pytest.raises(CommissioningPersistenceError):
        service(registry, path).inspect(runtime_revision="rev-1")
