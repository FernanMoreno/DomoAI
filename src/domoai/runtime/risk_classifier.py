"""Server-side authoritative risk classification.

The runtime never trusts a caller-supplied ``Command.risk_class`` alone.
``RiskClassifier`` computes an independent classification from
``(device, capability, command)``; ``PolicyEngine`` combines it with the
caller-supplied hint by taking the maximum (most restrictive) of the two, so
policy can only escalate risk, never downgrade it.

The default command table below is command-aware, not just device-type-aware:
a device type alone does not determine whether an action is safe, since e.g.
turning a light on and opening a cover are very different in consequence even
though both devices are otherwise unremarkable. Any ``(device type, command)``
combination not listed in the table fails closed to ``RESTRICTED``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from domoai.domain.models import Command, Device, DeviceType, RiskClass

_SAFE_COMMANDS: dict[DeviceType, frozenset[str]] = {
    DeviceType.LIGHT: frozenset({"turn_on", "turn_off", "toggle", "set_brightness"}),
    DeviceType.SWITCH: frozenset({"turn_on", "turn_off", "toggle"}),
}

_CONFIRM_COMMANDS: dict[DeviceType, frozenset[str]] = {
    DeviceType.COVER: frozenset({"open", "close", "set_position", "stop"}),
}

_RESTRICTED_COMMANDS: dict[DeviceType, frozenset[str]] = {
    DeviceType.SENSOR: frozenset({"*"}),
    DeviceType.ENERGY: frozenset({"*"}),
}

_CLIMATE_TEMPERATURE_COMMAND = "set_temperature"


@dataclass(frozen=True)
class RiskOverride:
    """One operator-configured override rule."""

    device_id: str | None = None
    area_id: str | None = None
    risk_class: RiskClass = RiskClass.RESTRICTED
    privileged_exception: bool = False


@dataclass
class RiskClassifier:
    """Authoritative, fail-closed risk classifier.

    Resolution order: device override, area override, default command
    table, then fail-closed ``RESTRICTED`` for anything unrecognized.
    """

    overrides: tuple[RiskOverride, ...] = field(default_factory=tuple)

    def classify(self, device: Device, capability: str, command: Command) -> RiskClass:
        default = self._classify_default(device, capability, command)
        for override in self.overrides:
            if override.device_id is not None and override.device_id == device.id:
                return self._effective_override(default, override)
        for override in self.overrides:
            if override.area_id is not None and override.area_id == device.area_id:
                return self._effective_override(default, override)
        return default

    @staticmethod
    def _effective_override(default: RiskClass, override: RiskOverride) -> RiskClass:
        # Deployment overrides may harden a decision. Lowering a restricted
        # or confirmation gate requires a separate privileged exception
        # contract, never an ordinary risk-overrides file.
        rank = {RiskClass.SAFE: 0, RiskClass.CONFIRM: 1, RiskClass.RESTRICTED: 2}
        if override.privileged_exception:
            return override.risk_class
        return (
            override.risk_class
            if rank[override.risk_class] >= rank[default]
            else default
        )

    @staticmethod
    def _classify_default(device: Device, capability: str, command: Command) -> RiskClass:
        device_type = device.type
        if device_type is DeviceType.UNSUPPORTED:
            return RiskClass.RESTRICTED
        if command.command in _SAFE_COMMANDS.get(device_type, frozenset()):
            return RiskClass.SAFE
        if command.command in _CONFIRM_COMMANDS.get(device_type, frozenset()):
            return RiskClass.CONFIRM
        if "*" in _RESTRICTED_COMMANDS.get(device_type, frozenset()):
            return RiskClass.RESTRICTED
        if device_type is DeviceType.CLIMATE and command.command == _CLIMATE_TEMPERATURE_COMMAND:
            return RiskClassifier._classify_climate_temperature(device, capability, command)
        if device_type is DeviceType.EV_CHARGER:
            return RiskClass.CONFIRM
        return RiskClass.RESTRICTED

    @staticmethod
    def _classify_climate_temperature(
        device: Device, capability: str, command: Command
    ) -> RiskClass:
        value = command.value
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return RiskClass.CONFIRM
        target = next((cap for cap in device.capabilities if cap.name == capability), None)
        if target is None or target.minimum is None or target.maximum is None:
            return RiskClass.CONFIRM
        if target.minimum <= value <= target.maximum:
            return RiskClass.SAFE
        return RiskClass.CONFIRM
