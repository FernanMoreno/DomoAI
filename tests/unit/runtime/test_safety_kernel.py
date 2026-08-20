from __future__ import annotations

from domoai.domain.errors import ErrorCode
from domoai.domain.models import DeviceType, SafetyLimit
from domoai.runtime.safety_kernel import SafetyKernel


def test_no_configured_limit_returns_none() -> None:
    kernel = SafetyKernel([])

    result = kernel.check(device_type=DeviceType.CLIMATE, capability="target_temperature", value=30)

    assert result is None


def test_none_value_returns_none() -> None:
    kernel = SafetyKernel(
        [SafetyLimit(device_type=DeviceType.CLIMATE, capability="target_temperature", maximum=28)]
    )

    result = kernel.check(
        device_type=DeviceType.CLIMATE, capability="target_temperature", value=None
    )

    assert result is None


def test_non_numeric_value_returns_none() -> None:
    kernel = SafetyKernel(
        [SafetyLimit(device_type=DeviceType.CLIMATE, capability="mode", maximum=28)]
    )

    result = kernel.check(device_type=DeviceType.CLIMATE, capability="mode", value="heat")

    assert result is None


def test_boolean_value_returns_none() -> None:
    kernel = SafetyKernel(
        [SafetyLimit(device_type=DeviceType.SWITCH, capability="power", maximum=1)]
    )

    result = kernel.check(device_type=DeviceType.SWITCH, capability="power", value=True)

    assert result is None


def test_below_minimum_returns_safety_limit_exceeded() -> None:
    kernel = SafetyKernel(
        [SafetyLimit(device_type=DeviceType.ENERGY, capability="battery_soc", minimum=10)]
    )

    result = kernel.check(device_type=DeviceType.ENERGY, capability="battery_soc", value=5)

    assert result is not None
    assert result.code == ErrorCode.SAFETY_LIMIT_EXCEEDED
    assert result.capability == "battery_soc"


def test_above_maximum_returns_safety_limit_exceeded() -> None:
    kernel = SafetyKernel(
        [SafetyLimit(device_type=DeviceType.EV_CHARGER, capability="charging_current", maximum=32)]
    )

    result = kernel.check(
        device_type=DeviceType.EV_CHARGER, capability="charging_current", value=40
    )

    assert result is not None
    assert result.code == ErrorCode.SAFETY_LIMIT_EXCEEDED


def test_within_bounds_returns_none() -> None:
    kernel = SafetyKernel(
        [
            SafetyLimit(
                device_type=DeviceType.CLIMATE,
                capability="target_temperature",
                minimum=10,
                maximum=28,
            )
        ]
    )

    result = kernel.check(device_type=DeviceType.CLIMATE, capability="target_temperature", value=20)

    assert result is None


def test_limit_scoped_to_device_type_does_not_apply_to_other_types() -> None:
    kernel = SafetyKernel(
        [SafetyLimit(device_type=DeviceType.CLIMATE, capability="value", maximum=10)]
    )

    result = kernel.check(device_type=DeviceType.COVER, capability="value", value=100)

    assert result is None
