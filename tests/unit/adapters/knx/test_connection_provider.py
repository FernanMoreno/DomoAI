from __future__ import annotations

import json
from pathlib import Path

import pytest

from domoai.adapters.knx.config import KnxMappingDocument
from domoai.adapters.knx.connection import KnxConnectionProvider
from domoai.application.connection_providers import ConnectionSetup
from domoai.domain.connections import (
    ConnectionCandidate,
    ConnectionDraft,
    DiagnosticSeverity,
    InventoryStatus,
)


def _mapping_path(tmp_path: Path) -> Path:
    mapping = KnxMappingDocument.model_validate(
        {
            "schema_version": "v1",
            "entities": [
                {
                    "entity_id": "light.kitchen",
                    "name": "Kitchen",
                    "semantic_type": "light",
                    "capabilities": [
                        {
                            "name": "power",
                            "dpt": "1.001",
                            "state_group_address": "1/0/1",
                            "command_group_address": "1/0/2",
                        }
                    ],
                }
            ],
        }
    )
    path = tmp_path / "knx.json"
    path.write_text(json.dumps(mapping.model_dump(mode="json")), encoding="utf-8")
    return path


def _setup(path: Path) -> ConnectionSetup:
    candidate = ConnectionCandidate(
        connector_id="knx",
        candidate_id="knx:gateway.local:3671",
        display_name="KNX gateway",
        endpoint={"host": "gateway.local", "port": 3671},
    )
    return ConnectionSetup(
        candidate=candidate,
        draft=ConnectionDraft(
            connector_id="knx",
            unique_id=candidate.candidate_id,
            endpoint=candidate.endpoint,
            options={"route_back": False},
            mapping_ref=str(path),
        ),
    )


@pytest.mark.asyncio
async def test_knx_provider_separates_transport_and_mapping_and_builds_adapter(
    tmp_path: Path,
) -> None:
    seen: list[tuple[str, int, bool, dict[str, str]]] = []

    def transport_factory(host: str, port: int, route_back: bool, group_dpts: dict[str, str]):
        seen.append((host, port, route_back, group_dpts))
        from domoai.adapters.knx.transport import InMemoryKnxTransport

        return InMemoryKnxTransport()

    provider = KnxConnectionProvider(transport_factory=transport_factory)
    validation = await provider.validate(_setup(_mapping_path(tmp_path)))
    assert validation.valid
    adapter = await provider.build_adapter(
        _setup(_mapping_path(tmp_path)), adapter_id="connection:knx-1"
    )

    assert adapter.adapter_id == "connection:knx-1"
    assert seen == [("gateway.local", 3671, False, {"1/0/1": "1.001"})]


@pytest.mark.asyncio
async def test_knx_provider_reports_mapping_required_and_hides_route_details(
    tmp_path: Path,
) -> None:
    provider = KnxConnectionProvider()
    candidate = provider.manual_candidate("gateway.local")
    assert "route_back" not in candidate.endpoint
    assert "route_back" not in candidate.metadata
    assert candidate.endpoint["port"] == 3671

    draft = ConnectionDraft(
        connector_id="knx",
        unique_id=candidate.candidate_id,
        endpoint=candidate.endpoint,
        options={},
    )
    validation = await provider.validate(ConnectionSetup(candidate=candidate, draft=draft))
    assert not validation.valid
    assert validation.diagnostics[0].code == "mapping_required"
    assert validation.diagnostics[0].severity is DiagnosticSeverity.ERROR


@pytest.mark.asyncio
async def test_knx_discovery_candidates_are_provider_scoped() -> None:
    async def discoverer() -> list[ConnectionCandidate]:
        return [
            ConnectionCandidate(
                connector_id="knx",
                candidate_id="knx:10.0.0.2:3671",
                display_name="KNX/IP",
                endpoint={"host": "10.0.0.2", "port": 3671},
                metadata={"interface": "tunnelling"},
            )
        ]

    provider = KnxConnectionProvider(gateway_discoverer=discoverer)
    candidates = await provider.discover()
    probe = await provider.probe(candidates[0])
    assert candidates[0].metadata["interface"] == "tunnelling"
    assert probe.inventory_status is InventoryStatus.MAPPING_REQUIRED


@pytest.mark.asyncio
async def test_knx_probe_reports_secure_and_exhausted_tunnel_requirements() -> None:
    provider = KnxConnectionProvider()
    secure = provider.manual_candidate("gateway.local").model_copy(
        update={"metadata": {"secure_required": True}}
    )
    secure_probe = await provider.probe(secure)
    assert secure_probe.diagnostics[0].code == "knx_secure_credentials_required"

    occupied = provider.manual_candidate("gateway.local").model_copy(
        update={"metadata": {"tunnel_slot_available": False}}
    )
    occupied_probe = await provider.probe(occupied)
    assert occupied_probe.diagnostics[0].code == "knx_no_tunnel_slot"
