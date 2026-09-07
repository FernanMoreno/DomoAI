from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from domoai.adapters.knx.config import KnxMappingDocument
from domoai.adapters.knx.mapper import KnxMapper
from domoai.adapters.knx.transport import InMemoryKnxTransport, KnxGroupValue, XknxTransport
from domoai.config.settings import Settings
from domoai.domain.models import Command
from tests.fixtures.knx import group_values, mapping_payload


def battery_mapping_payload() -> dict[str, object]:
    return {
        "schema_version": "v1",
        "entities": [
            {
                "entity_id": "lab.battery",
                "name": "Virtual Battery",
                "area_id": "lab",
                "semantic_type": "energy",
                "manufacturer": "DomoAI Lab",
                "model": "Deterministic Battery",
                "capabilities": [
                    {"name": "battery.soc", "dpt": "13.013", "state_group_address": "4/0/1"},
                    {
                        "name": "battery.power",
                        "dpt": "9.024",
                        "state_group_address": "4/0/2",
                        "command_group_address": "4/0/0",
                    },
                    {
                        "name": "battery.capacity",
                        "dpt": "13.013",
                        "state_group_address": "4/0/3",
                    },
                ],
            }
        ],
    }


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


def test_mapping_projects_explicit_energy_capabilities_and_routes() -> None:
    document = KnxMappingDocument.model_validate(battery_mapping_payload())
    battery = KnxMapper().to_snapshot(document).source_entities[0]
    capabilities = {item["name"]: item for item in battery["capabilities"]}

    assert battery["domain"] == "energy"
    assert capabilities["battery.soc"]["unit"] == "kWh"
    assert capabilities["battery.capacity"]["writable"] is False
    assert capabilities["battery.power"]["commands"] == [
        "charge_battery",
        "discharge_battery",
        "stop_battery",
    ]


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


def test_mapper_decodes_battery_energy_values() -> None:
    document = KnxMappingDocument.model_validate(battery_mapping_payload())
    mapper = KnxMapper()
    states = [
        mapper.decode(document, KnxGroupValue("4/0/1", "13.013", 5.0, datetime.now(UTC))),
        mapper.decode(document, KnxGroupValue("4/0/2", "9.024", -1.5, datetime.now(UTC))),
        mapper.decode(document, KnxGroupValue("4/0/3", "13.013", 10.0, datetime.now(UTC))),
    ]
    assert [(state["capability"], state["value"], state["unit"]) for state in states] == [
        ("battery.soc", 5.0, "kWh"),
        ("battery.power", -1.5, "kW"),
        ("battery.capacity", 10.0, "kWh"),
    ]


@pytest.mark.asyncio
async def test_adapter_translates_battery_dispatch_to_signed_knx_power() -> None:
    incoming = [
        KnxGroupValue("4/0/1", "13.013", 5.0, datetime.now(UTC)),
        KnxGroupValue("4/0/2", "9.024", 0.0, datetime.now(UTC)),
        KnxGroupValue("4/0/3", "13.013", 10.0, datetime.now(UTC)),
    ]
    transport = InMemoryKnxTransport(incoming=incoming)
    from domoai.adapters.knx.adapter import KnxAdapter

    adapter = KnxAdapter(transport, KnxMappingDocument.model_validate(battery_mapping_payload()))
    await adapter.connect()
    await adapter.discover()
    for command, value, unit, key in (
        ("charge_battery", 2.0, "kW", "charge"),
        ("discharge_battery", 1.5, "kW", "discharge"),
        ("stop_battery", None, None, "stop"),
    ):
        result = await adapter.execute(
            Command(
                id=f"battery-{key}",
                device_id="lab.virtual-battery",
                command=command,
                value=value,
                unit=unit,
                idempotency_key=f"battery-{key}-1",
            )
        )
        assert result.accepted is True

    assert [(write.group_address, write.dpt, write.value) for write in transport.writes] == [
        ("4/0/0", "9.024", 2.0),
        ("4/0/0", "9.024", -1.5),
        ("4/0/0", "9.024", 0.0),
    ]


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


@pytest.mark.asyncio
async def test_live_transport_preserves_textual_group_address_for_events() -> None:
    class _Destination:
        raw = 2049

        def __str__(self) -> str:
            return "1/0/1"

    class _Transcoder:
        def dpt_number_str(self) -> str:
            return "1.001"

    class _Decoded:
        transcoder = _Transcoder()
        value = True

    class _Telegram:
        destination_address = _Destination()
        decoded_data = _Decoded()

    transport = XknxTransport("192.0.2.10")
    transport._xknx = object()
    transport._on_telegram(_Telegram())

    event = await transport.receive(0.01)
    assert event is not None
    assert event.group_address == "1/0/1"


def test_live_transport_answers_registered_group_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xknx.telegram.apci import GroupValueRead

    calls: list[tuple[object, str, float, str]] = []

    def fake_response(
        xknx: object, address: str, value: float, *, value_type: str
    ) -> None:
        calls.append((xknx, address, value, value_type))

    import xknx.tools

    monkeypatch.setattr(xknx.tools, "group_value_response", fake_response)

    class _Destination:
        def __str__(self) -> str:
            return "4/0/2"

    class _Telegram:
        destination_address = _Destination()
        payload = GroupValueRead()
        decoded_data = None

    xknx_instance = object()
    transport = XknxTransport("192.0.2.10")
    transport._xknx = xknx_instance
    transport.set_group_read_response("4/0/2", "9.024", 1.25)
    transport._on_telegram(_Telegram())

    assert calls == [(xknx_instance, "4/0/2", 1.25, "9.024")]


@pytest.mark.asyncio
async def test_live_transport_passes_route_back_to_xknx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import xknx
    import xknx.io

    captured: dict[str, object] = {}

    class _Config:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    class _DptRegistry:
        def set(self, _group_dpts: dict[str, str]) -> None:
            return None

    class _TelegramQueue:
        def register_telegram_received_cb(self, _callback: object, **_kwargs: object) -> object:
            return object()

        def unregister_telegram_received_cb(self, _callback: object) -> None:
            return None

    class _Xknx:
        def __init__(self, *, connection_config: _Config) -> None:
            self.connection_config = connection_config
            self.group_address_dpt = _DptRegistry()
            self.telegram_queue = _TelegramQueue()

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(xknx, "XKNX", _Xknx)
    monkeypatch.setattr(xknx.io, "ConnectionConfig", _Config)

    transport = XknxTransport("192.0.2.10", route_back=True)
    await transport.connect()
    await transport.disconnect()

    assert captured["route_back"] is True
