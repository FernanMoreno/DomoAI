"""Strict, credential-free configuration for Home Assistant Provider mappings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from domoai.domain.models import StrictModel
from domoai.domain.provider import NominalCapacityAttestation


class HomeAssistantMappingConfigurationError(ValueError):
    """Raised when a local Home Assistant mapping document is not safe to use."""


class HomeAssistantIdentityClaims(StrictModel):
    """Stable Home Assistant registry claims used to resolve a live device ID."""

    identity_keys: list[str] = Field(default_factory=list, max_length=32)
    connections: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_claims(self) -> HomeAssistantIdentityClaims:
        claims = [*self.identity_keys, *self.connections]
        if not claims or any(not claim.strip() for claim in claims):
            raise ValueError("identity claim must include a non-empty identity key or connection")
        if len(set(claims)) != len(claims):
            raise ValueError("identity claims must be unique")
        return self


class HomeAssistantBatteryCapacityBinding(StrictModel):
    """Explicit nominal-capacity entity binding for one HA device."""

    device_id: str | None = Field(default=None, min_length=1, max_length=256)
    identity_claims: HomeAssistantIdentityClaims | None = None
    semantics: Literal["nominal_capacity"] = "nominal_capacity"
    nominal_capacity_attestation: NominalCapacityAttestation

    @model_validator(mode="after")
    def validate_binding_identity(self) -> HomeAssistantBatteryCapacityBinding:
        if self.device_id is None and self.identity_claims is None:
            raise ValueError("capacity binding requires device_id or identity claims")
        return self


_HOME_ASSISTANT_ENTITY_ID_PATTERN = r"^[a-z0-9_]+\.[a-z0-9_]+$"
_PROVIDER_COMMAND_PATTERN = r"^[a-z0-9][a-z0-9_.-]*$"


class HomeAssistantBatteryCommandRoute(StrictModel):
    """One exact Home Assistant source entity and provider command name."""

    entity_id: str = Field(pattern=_HOME_ASSISTANT_ENTITY_ID_PATTERN)
    provider_command: str = Field(
        min_length=1,
        max_length=128,
        pattern=_PROVIDER_COMMAND_PATTERN,
    )
    service_domain: Literal["number"] | None = None
    service: Literal["set_value"] | None = None
    value_transform: Literal["none", "as_is", "negate", "zero"] = "none"

    @model_validator(mode="after")
    def validate_service_route(self) -> HomeAssistantBatteryCommandRoute:
        if (self.service_domain is None) != (self.service is None):
            raise ValueError("service_domain and service must be configured together")
        if self.service == "set_value" and self.value_transform == "none":
            raise ValueError("number.set_value routes require an explicit value_transform")
        if self.service is None and self.value_transform != "none":
            raise ValueError("value_transform requires an explicit numeric service route")
        return self


class HomeAssistantDispatchableBatteryBinding(StrictModel):
    """Static HA-side routes for a future dispatchable battery composition."""

    schema_version: Literal["v1"] = "v1"
    device_id: str | None = Field(default=None, min_length=1, max_length=256)
    # This is an explicit cross-adapter identity owned by the deployment
    # mapping.  It is never inferred from names, entity IDs or telemetry and
    # does not by itself authorize actuator writes.
    canonical_device_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=_PROVIDER_COMMAND_PATTERN,
    )
    identity_claims: HomeAssistantIdentityClaims | None = None
    control_capability: str = Field(
        default="battery_control",
        min_length=1,
        max_length=128,
        pattern=_PROVIDER_COMMAND_PATTERN,
    )
    soc_entity_id: str = Field(pattern=_HOME_ASSISTANT_ENTITY_ID_PATTERN)
    power_feedback_entity_id: str = Field(pattern=_HOME_ASSISTANT_ENTITY_ID_PATTERN)
    capacity_entity_id: str = Field(pattern=_HOME_ASSISTANT_ENTITY_ID_PATTERN)
    soc_capability: Literal["battery.soc"] = "battery.soc"
    soc_unit: Literal["%"] = "%"
    power_feedback_capability: Literal["battery.power"] = "battery.power"
    power_unit: Literal["kW"] = "kW"
    capacity_metric: Literal["battery.capacity"] = "battery.capacity"
    charge: HomeAssistantBatteryCommandRoute
    discharge: HomeAssistantBatteryCommandRoute
    stop: HomeAssistantBatteryCommandRoute

    @model_validator(mode="after")
    def validate_distinct_commands(self) -> HomeAssistantDispatchableBatteryBinding:
        if self.device_id is None and self.identity_claims is None:
            raise ValueError("dispatch binding requires device_id or identity claims")
        commands = {
            self.charge.provider_command,
            self.discharge.provider_command,
            self.stop.provider_command,
        }
        if len(commands) != 3:
            raise ValueError("battery command routes must use distinct provider commands")
        return self


class HomeAssistantEVChargingBinding(StrictModel):
    """Static HA-side routes for one explicitly bound EV charger."""

    schema_version: Literal["v1"] = "v1"
    device_id: str | None = Field(default=None, min_length=1, max_length=256)
    canonical_device_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=_PROVIDER_COMMAND_PATTERN,
    )
    identity_claims: HomeAssistantIdentityClaims | None = None
    soc_entity_id: str = Field(pattern=_HOME_ASSISTANT_ENTITY_ID_PATTERN)
    power_feedback_entity_id: str = Field(pattern=_HOME_ASSISTANT_ENTITY_ID_PATTERN)
    capacity_entity_id: str = Field(pattern=_HOME_ASSISTANT_ENTITY_ID_PATTERN)
    connected_entity_id: str = Field(pattern=_HOME_ASSISTANT_ENTITY_ID_PATTERN)
    soc_capability: Literal["ev.soc"] = "ev.soc"
    # HA commonly publishes EV SOC as a percentage.  The runtime state
    # provider converts that declared source unit to canonical kWh using the
    # bound capacity; it never guesses this from a numeric value.
    soc_unit: Literal["kWh", "%"] = "kWh"
    power_feedback_capability: Literal["ev_charging"] = "ev_charging"
    power_unit: Literal["kW"] = "kW"
    capacity_metric: Literal["ev.capacity"] = "ev.capacity"
    capacity_unit: Literal["kWh"] = "kWh"
    connected_capability: Literal["ev.connected"] = "ev.connected"
    charge: HomeAssistantBatteryCommandRoute
    stop: HomeAssistantBatteryCommandRoute

    @model_validator(mode="after")
    def validate_routes(self) -> HomeAssistantEVChargingBinding:
        if self.device_id is None and self.identity_claims is None:
            raise ValueError("EV charging binding requires device_id or identity claims")
        if self.charge.provider_command == self.stop.provider_command:
            raise ValueError("EV command routes must use distinct provider commands")
        telemetry_entities = {
            self.soc_entity_id,
            self.power_feedback_entity_id,
            self.capacity_entity_id,
            self.connected_entity_id,
        }
        if len(telemetry_entities) != 4:
            raise ValueError("EV telemetry entities must be distinct")
        return self


class HomeAssistantMappingDocument(StrictModel):
    schema_version: Literal["v1"]
    metric_mappings: dict[str, dict[str, str]] = Field(default_factory=dict)
    battery_capacity_bindings: dict[str, HomeAssistantBatteryCapacityBinding] = Field(
        default_factory=dict
    )
    battery_dispatch_bindings: dict[str, HomeAssistantDispatchableBatteryBinding] = Field(
        default_factory=dict
    )
    ev_charging_bindings: dict[str, HomeAssistantEVChargingBinding] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_mappings(self) -> HomeAssistantMappingDocument:
        for entity_id, mapping in self.metric_mappings.items():
            if not entity_id.strip() or not mapping:
                raise ValueError("metric mappings require a Home Assistant entity and entries")
            for capability, metric in mapping.items():
                if not capability.strip() or not metric.strip():
                    raise ValueError("metric mapping keys and values must be non-empty")
        for entity_id, binding in self.battery_capacity_bindings.items():
            if not entity_id.strip():
                raise ValueError("capacity bindings require a Home Assistant entity")
            if entity_id in self.metric_mappings:
                raise ValueError("capacity entity cannot overlap metric_mappings")
            if binding.device_id is None and binding.identity_claims is None:
                raise ValueError("capacity bindings require a Home Assistant device identity")
        canonical_device_ids: set[str] = set()
        for binding_id, dispatch_binding in self.battery_dispatch_bindings.items():
            if not binding_id.strip():
                raise ValueError("dispatch bindings require a stable binding ID")
            if dispatch_binding.canonical_device_id is not None:
                if dispatch_binding.canonical_device_id in canonical_device_ids:
                    raise ValueError("dispatch bindings must use unique canonical device IDs")
                canonical_device_ids.add(dispatch_binding.canonical_device_id)
            capacity_binding = self.battery_capacity_bindings.get(
                dispatch_binding.capacity_entity_id
            )
            if capacity_binding is None:
                raise ValueError("dispatch binding capacity entity must have a capacity binding")
            if capacity_binding.identity_claims != dispatch_binding.identity_claims:
                raise ValueError("dispatch binding capacity entity must match the identity claims")
            if (
                capacity_binding.identity_claims is None
                and capacity_binding.device_id != dispatch_binding.device_id
            ):
                raise ValueError("dispatch binding capacity entity must match the binding device")
        for binding_id, ev_binding in self.ev_charging_bindings.items():
            if not binding_id.strip():
                raise ValueError("EV charging bindings require a stable binding ID")
            if ev_binding.canonical_device_id is not None:
                if ev_binding.canonical_device_id in canonical_device_ids:
                    raise ValueError("all actuator bindings must use unique canonical device IDs")
                canonical_device_ids.add(ev_binding.canonical_device_id)
        return self


def load_home_assistant_mapping(path: Path) -> HomeAssistantMappingDocument:
    """Load one strict local mapping document without exposing parse details."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return HomeAssistantMappingDocument.model_validate(payload)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValidationError,
        TypeError,
        ValueError,
    ) as error:
        raise HomeAssistantMappingConfigurationError(
            "Home Assistant mapping file is unavailable or not valid v1 JSON"
        ) from error


def load_metric_mappings(path: Path) -> dict[str, dict[str, str]]:
    """Load generic semantic metric mappings from one strict document."""

    return load_home_assistant_mapping(path).metric_mappings


def load_battery_capacity_bindings(
    path: Path,
) -> dict[str, HomeAssistantBatteryCapacityBinding]:
    """Load explicit nominal-capacity entity bindings."""

    return load_home_assistant_mapping(path).battery_capacity_bindings


def load_battery_dispatch_bindings(
    path: Path,
) -> dict[str, HomeAssistantDispatchableBatteryBinding]:
    """Load explicit, inert Home Assistant battery route declarations."""

    return load_home_assistant_mapping(path).battery_dispatch_bindings


def load_ev_charging_bindings(
    path: Path,
) -> dict[str, HomeAssistantEVChargingBinding]:
    """Load explicit, inert Home Assistant EV route declarations."""

    return load_home_assistant_mapping(path).ev_charging_bindings


__all__ = [
    "HomeAssistantBatteryCapacityBinding",
    "HomeAssistantBatteryCommandRoute",
    "HomeAssistantDispatchableBatteryBinding",
    "HomeAssistantEVChargingBinding",
    "HomeAssistantIdentityClaims",
    "HomeAssistantMappingConfigurationError",
    "HomeAssistantMappingDocument",
    "load_battery_capacity_bindings",
    "load_battery_dispatch_bindings",
    "load_ev_charging_bindings",
    "load_home_assistant_mapping",
    "load_metric_mappings",
]
