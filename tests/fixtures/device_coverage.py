"""Local route fixtures for the public coverage evidence resource."""

from typing import Any

from domoai.domain.models import AdapterSnapshot

RESOURCE = "coverage-evidence"
UNKNOWN_REASONS = [
    "evidence_not_bound",
    "protocol_unverified",
    "profile_unverified",
    "firmware_unverified",
    "identity_unverified",
    "tested_at_unverified",
    "expiry_unverified",
]


def capability(
    name: str,
    *,
    writable: bool = False,
    unit: str | None = None,
    commands: list[str] | None = None,
    kind: str = "number",
) -> dict[str, Any]:
    return {
        "name": name,
        "kind": kind,
        "readable": True,
        "writable": writable,
        "unit": unit,
        "commands": commands or [],
    }


def family_snapshot() -> AdapterSnapshot:
    entities = [
        ("light", "light", [capability("power", writable=True, kind="boolean")]),
        ("switch", "switch", [capability("power", writable=True, kind="boolean")]),
        ("metered", "switch", [capability("energy", unit="kWh")]),
        ("blind", "cover", [capability("position", writable=True, commands=["set_position"])]),
        ("climate", "climate", [capability("temperature")]),
        ("sensor", "sensor", [capability("power", unit="W")]),
    ]
    return AdapterSnapshot(
        source_entities=[
            {
                "entity_id": f"home.{name}",
                "canonical_id": f"home.{name}",
                "name": name,
                "semantic_type": kind,
                "capabilities": capabilities,
                "area_id": "home.room",
                "available": True,
            }
            for name, kind, capabilities in entities
        ]
    )


def assert_unknown(row: dict[str, Any]) -> None:
    assert row["evidence_level"] == "unknown"
    assert row["evidence_status"] == "not_verified"
    assert row["physical_identity_status"] == "unverified"
    for route in row["routes"]:
        assert route["evidence"] == {
            "level": "unknown",
            "status": "not_verified",
            "protocol": None,
            "profile": None,
            "firmware": None,
            "hardware_identity": None,
            "tested_at": None,
            "expires_at": None,
            "verified_capabilities": [],
            "verified_operations": [],
            "reasons": UNKNOWN_REASONS,
        }
