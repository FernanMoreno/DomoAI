from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from domoai.adapters.knx.config import KnxMappingDocument
from domoai.adapters.knx.mapper import KnxMapper
from domoai.adapters.knx.transport import InMemoryKnxTransport, KnxGroupValue
from domoai.config.settings import Settings
from tests.fixtures.knx import group_values, mapping_payload


def test_mapping_projects_bounded_light_switch_and_sensor_capabilities() -> None:
    document = KnxMappingDocument.model_validate(mapping_payload())
    snapshot = KnxMapper().to_snapshot(document)

    light = next(entity for entity in snapshot.source_entities if entity["domain"] == "light")
    sensor = next(entity for entity in snapshot.source_entities if entity["domain"] == "sensor")

    assert {capability["name"] for capability in light["capabilities"]} == {
        "power",
        "brightness",
    }
    assert light["capabilities"][0]["commands"] == ["turn_on", "turn_off"]
    assert {capability["name"] for capability in sensor["capabilities"]} == {
        "temperature",
        "humidity",
        "occupancy",
    }
    assert all(not capability["writable"] for capability in sensor["capabilities"])


@pytest.mark.parametrize(
    "mutator",
    [
        lambda payload: payload["entities"][0].update({"unexpected": True}),
        lambda payload: payload["entities"][0]["capabilities"][0].update({"dpt": "9.001"}),
        lambda payload: payload["entities"][0]["capabilities"][0].update(
            {"state_group_address": "16/0/1"}
        ),
        lambda payload: payload["entities"][0]["capabilities"][1].update(
            {"command_group_address": "i-1/0/2"}
        ),
    ],
)
def test_mapping_rejects_unknown_or_unsafe_values(mutator: object) -> None:
    payload = mapping_payload()
    assert callable(mutator)
    mutator(payload)

    with pytest.raises(ValueError):
        KnxMappingDocument.model_validate(payload)


def test_mapping_rejects_duplicate_entity_and_derived_canonical_identity() -> None:
    duplicate_entity = mapping_payload()
    duplicate_entity["entities"].append(duplicate_entity["entities"][0].copy())

    with pytest.raises(ValueError, match="duplicate"):
        KnxMappingDocument.model_validate(duplicate_entity)

    duplicate_derived = mapping_payload()
    duplicate_derived["entities"][1]["name"] = "Main Light"
    duplicate_derived["entities"][1]["area_id"] = "living_room"

    with pytest.raises(ValueError, match="canonical"):
        KnxMappingDocument.model_validate(duplicate_derived)


def test_mapper_decodes_supported_dpts_and_rejects_invalid_ranges() -> None:
    document = KnxMappingDocument.model_validate(mapping_payload())
    mapper = KnxMapper()

    states = [mapper.decode(document, value) for value in group_values()]
    assert {(state["capability"], state["value"], state["unit"]) for state in states} == {
        ("power", True, None),
        ("brightness", 50, "%"),
        ("power", False, None),
        ("temperature", 21.5, "°C"),
        ("humidity", 42.5, "%"),
        ("occupancy", True, None),
    }

    with pytest.raises(ValueError, match="humidity"):
        mapper.decode(document, KnxGroupValue("2/1/2", "9.007", 101, datetime.now(UTC)))

    with pytest.raises(ValueError, match="unknown"):
        mapper.decode(document, KnxGroupValue("9/9/9", "1.001", True, datetime.now(UTC)))


@pytest.mark.asyncio
async def test_in_memory_transport_records_io_and_delivers_events() -> None:
    transport = InMemoryKnxTransport(incoming=group_values())

    await transport.connect()
    result = await transport.receive(0.01)
    await transport.read_group("1/0/1", "1.001")
    await transport.write_group("1/0/0", "1.001", True)

    assert result == group_values()[0]
    assert transport.reads == [("1/0/1", "1.001")]
    assert transport.writes[-1].group_address == "1/0/0"
    assert transport.writes[-1].dpt == "1.001"
    assert transport.writes[-1].value is True
    assert await transport.health() is True

    await transport.disconnect()
    assert await transport.health() is False


def test_knx_settings_require_complete_and_allow_composition() -> None:
    settings = Settings(knx_gateway_host="192.0.2.10", knx_config_path=Path("config/knx.json"))
    assert settings.knx_timeout_seconds == 5.0

    with pytest.raises(ValueError, match="DOMOAI_KNX_GATEWAY_HOST"):
        Settings(knx_gateway_host="192.0.2.10")
    with pytest.raises(ValueError, match="DOMOAI_KNX_CONFIG_PATH"):
        Settings(knx_config_path=Path("config/knx.json"))
    composed = Settings(
        home_assistant_url="http://home-assistant.test",
        home_assistant_token=SecretStr("fixture-token"),
        knx_gateway_host="192.0.2.10",
        knx_config_path=Path("config/knx.json"),
    )
    assert composed.knx_gateway_host is not None
