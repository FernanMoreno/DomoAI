from typing import Any

from domoai.domain.models import (
    Capability,
    CapabilityKind,
    Command,
    Device,
    DeviceType,
    RiskClass,
    SourceRef,
)
from domoai.runtime.risk_classifier import RiskClassifier, RiskOverride


def _device(
    device_id: str,
    device_type: DeviceType,
    *,
    area_id: str | None = None,
    capabilities: list[Capability] | None = None,
) -> Device:
    return Device(
        id=device_id,
        type=device_type,
        name=device_id,
        area_id=area_id,
        protocol="fixture",
        source_refs=[SourceRef(adapter_id="fixture", external_id=device_id)],
        capabilities=capabilities
        or [
            Capability(
                name="power",
                kind=CapabilityKind.BOOLEAN,
                readable=True,
                writable=True,
                commands=["turn_on", "open"],
            )
        ],
    )


def _climate_device(minimum: float | None, maximum: float | None) -> Device:
    return _device(
        "climate.bedroom",
        DeviceType.CLIMATE,
        capabilities=[
            Capability(
                name="target_temperature",
                kind=CapabilityKind.NUMBER,
                readable=True,
                writable=True,
                minimum=minimum,
                maximum=maximum,
                commands=["set_temperature"],
            )
        ],
    )


def _command(command_name: str = "turn_on", *, value: Any = None) -> Command:
    return Command(
        id="cmd-1",
        device_id="device-1",
        command=command_name,
        value=value,
        idempotency_key="key-1",
    )


def test_known_device_type_defaults_to_safe() -> None:
    classifier = RiskClassifier()
    device = _device("light.kitchen", DeviceType.LIGHT)

    assert classifier.classify(device, "power", _command()) is RiskClass.SAFE


def test_unsupported_device_type_fails_closed_to_restricted() -> None:
    classifier = RiskClassifier()
    device = _device("unknown.thing", DeviceType.UNSUPPORTED)

    assert classifier.classify(device, "power", _command()) is RiskClass.RESTRICTED


def test_device_override_takes_precedence() -> None:
    classifier = RiskClassifier(
        overrides=(RiskOverride(device_id="cover.garage_main", risk_class=RiskClass.RESTRICTED),)
    )
    device = _device("cover.garage_main", DeviceType.COVER)

    assert classifier.classify(device, "power", _command("open")) is RiskClass.RESTRICTED


def test_area_override_applies_when_no_device_override_matches() -> None:
    classifier = RiskClassifier(
        overrides=(RiskOverride(area_id="exterior", risk_class=RiskClass.CONFIRM),)
    )
    device = _device("cover.side_gate", DeviceType.COVER, area_id="exterior")

    assert classifier.classify(device, "power", _command("open")) is RiskClass.CONFIRM


def test_device_override_wins_over_area_override() -> None:
    classifier = RiskClassifier(
        overrides=(
            RiskOverride(area_id="exterior", risk_class=RiskClass.CONFIRM),
            RiskOverride(device_id="cover.garage_main", risk_class=RiskClass.RESTRICTED),
        )
    )
    device = _device("cover.garage_main", DeviceType.COVER, area_id="exterior")

    assert classifier.classify(device, "power", _command("open")) is RiskClass.RESTRICTED


def test_cover_commands_default_to_confirm() -> None:
    classifier = RiskClassifier()
    device = _device("cover.bedroom_blind", DeviceType.COVER)

    for command_name in ("open", "close", "set_position", "stop"):
        assert (
            classifier.classify(device, "position", _command(command_name)) is RiskClass.CONFIRM
        ), command_name


def test_switch_commands_default_to_safe() -> None:
    classifier = RiskClassifier()
    device = _device("switch.kitchen", DeviceType.SWITCH)

    for command_name in ("turn_on", "turn_off", "toggle"):
        assert classifier.classify(device, "power", _command(command_name)) is RiskClass.SAFE, (
            command_name
        )


def test_light_set_brightness_defaults_to_safe() -> None:
    classifier = RiskClassifier()
    device = _device("light.kitchen", DeviceType.LIGHT)

    assert classifier.classify(device, "brightness", _command("set_brightness")) is RiskClass.SAFE


def test_sensor_and_energy_commands_default_to_restricted() -> None:
    classifier = RiskClassifier()
    sensor = _device("sensor.temperature", DeviceType.SENSOR)
    energy = _device("energy.meter", DeviceType.ENERGY)

    assert classifier.classify(sensor, "value", _command("refresh")) is RiskClass.RESTRICTED
    assert classifier.classify(energy, "value", _command("refresh")) is RiskClass.RESTRICTED


def test_ev_charger_commands_default_to_confirm() -> None:
    classifier = RiskClassifier()
    device = _device("ev_charger.garage", DeviceType.EV_CHARGER)

    assert (
        classifier.classify(device, "charge_power", _command("start_charge")) is RiskClass.CONFIRM
    )


def test_unrecognized_command_on_known_device_type_fails_closed() -> None:
    classifier = RiskClassifier()
    device = _device("light.kitchen", DeviceType.LIGHT)

    assert classifier.classify(device, "power", _command("factory_reset")) is RiskClass.RESTRICTED


def test_climate_temperature_within_envelope_defaults_to_safe() -> None:
    classifier = RiskClassifier()
    device = _climate_device(minimum=16, maximum=27)

    for value in (16, 21, 27):
        assert (
            classifier.classify(
                device, "target_temperature", _command("set_temperature", value=value)
            )
            is RiskClass.SAFE
        ), value


def test_climate_temperature_outside_envelope_defaults_to_confirm() -> None:
    classifier = RiskClassifier()
    device = _climate_device(minimum=16, maximum=27)

    for value in (5, 35):
        assert (
            classifier.classify(
                device, "target_temperature", _command("set_temperature", value=value)
            )
            is RiskClass.CONFIRM
        ), value


def test_climate_temperature_with_no_configured_envelope_defaults_to_confirm() -> None:
    classifier = RiskClassifier()
    device = _climate_device(minimum=None, maximum=None)

    assert (
        classifier.classify(device, "target_temperature", _command("set_temperature", value=21))
        is RiskClass.CONFIRM
    )


def test_climate_temperature_with_non_numeric_value_defaults_to_confirm() -> None:
    classifier = RiskClassifier()
    device = _climate_device(minimum=16, maximum=27)

    string_command = _command("set_temperature", value="warm")
    bool_command = _command("set_temperature", value=True)

    assert classifier.classify(device, "target_temperature", string_command) is RiskClass.CONFIRM
    assert classifier.classify(device, "target_temperature", bool_command) is RiskClass.CONFIRM


def test_override_forces_safe_on_a_device_whose_default_is_confirm() -> None:
    classifier = RiskClassifier(
        overrides=(RiskOverride(device_id="cover.garage_main", risk_class=RiskClass.SAFE),)
    )
    device = _device("cover.garage_main", DeviceType.COVER)

    assert classifier.classify(device, "position", _command("open")) is RiskClass.SAFE
