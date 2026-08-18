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


def _device(device_id: str, device_type: DeviceType, *, area_id: str | None = None) -> Device:
    return Device(
        id=device_id,
        type=device_type,
        name=device_id,
        area_id=area_id,
        protocol="fixture",
        source_refs=[SourceRef(adapter_id="fixture", external_id=device_id)],
        capabilities=[
            Capability(
                name="power",
                kind=CapabilityKind.BOOLEAN,
                readable=True,
                writable=True,
                commands=["turn_on", "open"],
            )
        ],
    )


def _command(command_name: str = "turn_on") -> Command:
    return Command(
        id="cmd-1",
        device_id="device-1",
        command=command_name,
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
