"""Independent, server-owned hard limits, separate from policy and adapters."""

from __future__ import annotations

from collections.abc import Sequence

from domoai.domain.errors import ErrorCode
from domoai.domain.models import DeviceType, ErrorDetail, SafetyLimit

_DEFAULT_LIMITS = (
    SafetyLimit(device_type=DeviceType.LIGHT, capability="brightness", minimum=0, maximum=100),
    SafetyLimit(device_type=DeviceType.COVER, capability="position", minimum=0, maximum=100),
    SafetyLimit(device_type=DeviceType.ENERGY, capability="battery_soc", minimum=0, maximum=100),
)


class SafetyKernel:
    """Decide hard physical limits, independent of capabilities and policy.

    The normalized percentage-like capabilities have conservative built-in
    bounds. Deployment configuration may tighten those bounds, but cannot
    widen them. Device-specific limits for other capabilities remain an
    explicit deployment responsibility because the runtime cannot infer a
    safe electrical or thermal maximum from a semantic name alone.
    """

    def __init__(self, limits: Sequence[SafetyLimit]) -> None:
        self._limits = {
            (limit.device_type, limit.capability): limit for limit in _DEFAULT_LIMITS
        }
        for limit in limits:
            key = (limit.device_type, limit.capability)
            default = self._limits.get(key)
            if default is None:
                self._limits[key] = limit
                continue
            self._limits[key] = SafetyLimit(
                device_type=limit.device_type,
                capability=limit.capability,
                minimum=_stricter_minimum(default.minimum, limit.minimum),
                maximum=_stricter_maximum(default.maximum, limit.maximum),
            )

    def check(
        self, *, device_type: DeviceType, capability: str, value: object
    ) -> ErrorDetail | None:
        limit = self._limits.get((device_type, capability))
        if limit is None or value is None:
            return None
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        if limit.minimum is not None and value < limit.minimum:
            return ErrorDetail(
                code=ErrorCode.SAFETY_LIMIT_EXCEEDED,
                message=f"Value {value} is below the hard safety minimum {limit.minimum}",
                capability=capability,
                retryable=False,
            )
        if limit.maximum is not None and value > limit.maximum:
            return ErrorDetail(
                code=ErrorCode.SAFETY_LIMIT_EXCEEDED,
                message=f"Value {value} exceeds the hard safety maximum {limit.maximum}",
                capability=capability,
                retryable=False,
            )
        return None


def _stricter_minimum(
    default: float | int | None, configured: float | int | None
) -> float | int | None:
    if default is None:
        return configured
    if configured is None:
        return default
    return max(default, configured)


def _stricter_maximum(
    default: float | int | None, configured: float | int | None
) -> float | int | None:
    if default is None:
        return configured
    if configured is None:
        return default
    return min(default, configured)
