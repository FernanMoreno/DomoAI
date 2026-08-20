"""Independent, server-owned hard limits, separate from policy and adapters."""

from __future__ import annotations

from collections.abc import Sequence

from domoai.domain.errors import ErrorCode
from domoai.domain.models import DeviceType, ErrorDetail, SafetyLimit


class SafetyKernel:
    """Decides hard physical limits, independent of Capability bounds and policy."""

    def __init__(self, limits: Sequence[SafetyLimit]) -> None:
        self._limits: dict[tuple[DeviceType, str], SafetyLimit] = {
            (limit.device_type, limit.capability): limit for limit in limits
        }

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
