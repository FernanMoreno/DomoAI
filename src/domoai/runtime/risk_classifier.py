"""Server-side authoritative risk classification.

The runtime never trusts a caller-supplied ``Command.risk_class`` alone.
``RiskClassifier`` computes an independent classification from
``(device, capability, command)``; ``PolicyEngine`` combines it with the
caller-supplied hint by taking the maximum (most restrictive) of the two, so
policy can only escalate risk, never downgrade it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from domoai.domain.models import Command, Device, DeviceType, RiskClass


@dataclass(frozen=True)
class RiskOverride:
    """One operator-configured override rule."""

    device_id: str | None = None
    area_id: str | None = None
    risk_class: RiskClass = RiskClass.RESTRICTED


@dataclass
class RiskClassifier:
    """Authoritative, fail-closed risk classifier.

    Resolution order: device override, area override, default command
    table, then fail-closed ``RESTRICTED`` for anything unrecognized.
    """

    overrides: tuple[RiskOverride, ...] = field(default_factory=tuple)

    def classify(self, device: Device, capability: str, command: Command) -> RiskClass:
        del capability, command
        for override in self.overrides:
            if override.device_id is not None and override.device_id == device.id:
                return override.risk_class
        for override in self.overrides:
            if override.area_id is not None and override.area_id == device.area_id:
                return override.risk_class
        return self._classify_default(device.type)

    @staticmethod
    def _classify_default(device_type: DeviceType) -> RiskClass:
        if device_type is DeviceType.UNSUPPORTED:
            return RiskClass.RESTRICTED
        return RiskClass.SAFE
