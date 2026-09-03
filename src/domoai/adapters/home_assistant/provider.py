"""Home Assistant implementation of the Provider SDK v1 boundary."""

from __future__ import annotations

import math
from collections.abc import AsyncIterator, Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from domoai.adapters.home_assistant.client import HomeAssistantClient
from domoai.adapters.home_assistant.config import (
    HomeAssistantBatteryCapacityBinding,
    HomeAssistantBatteryCommandRoute,
    HomeAssistantDispatchableBatteryBinding,
    HomeAssistantEVChargingBinding,
    HomeAssistantIdentityClaims,
    HomeAssistantMappingConfigurationError,
)
from domoai.adapters.home_assistant.mapper import HomeAssistantMapper
from domoai.domain.models import (
    AdapterSnapshot,
    Capability,
    CapabilityKind,
    DeviceType,
    ExecutionStatus,
    SourceRef,
)
from domoai.domain.provider import (
    DeviceDescriptor,
    Measurement,
    MeasurementQuality,
    ProviderCommand,
    ProviderExecutionResult,
    ProviderManifest,
    ProviderRole,
)
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.execution_context import ExecutionContext


def _manifest() -> ProviderManifest:
    return ProviderManifest(
        provider_id="home_assistant",
        name="Home Assistant Provider",
        protocol="home_assistant",
        package_name="domoai",
        package_version="0.1.0",
        roles=[ProviderRole.TELEMETRY, ProviderRole.COMMANDS],
        device_types=[
            DeviceType.LIGHT,
            DeviceType.SWITCH,
            DeviceType.COVER,
            DeviceType.CLIMATE,
            DeviceType.SENSOR,
            DeviceType.ENERGY,
        ],
        capabilities=[
            Capability(
                name="power",
                kind=CapabilityKind.BOOLEAN,
                readable=True,
                writable=True,
                commands=["turn_on", "turn_off", "toggle"],
            ),
            Capability(
                name="brightness",
                kind=CapabilityKind.INTEGER,
                unit="%",
                readable=True,
                writable=True,
                minimum=0,
                maximum=100,
                commands=["set_brightness"],
            ),
            Capability(
                name="position",
                kind=CapabilityKind.INTEGER,
                unit="%",
                readable=True,
                writable=True,
                minimum=0,
                maximum=100,
                commands=["set_position", "open", "close", "stop"],
            ),
            Capability(
                name="temperature",
                kind=CapabilityKind.NUMBER,
                unit="°C",
                readable=True,
                writable=False,
            ),
            Capability(
                name="target_temperature",
                kind=CapabilityKind.NUMBER,
                unit="°C",
                readable=True,
                writable=True,
                minimum=16,
                maximum=27,
                commands=["set_temperature"],
            ),
            Capability(
                name="energy.pv.power",
                kind=CapabilityKind.NUMBER,
                unit="W",
                readable=True,
                writable=False,
            ),
            Capability(
                name="energy.grid.power",
                kind=CapabilityKind.NUMBER,
                unit="W",
                readable=True,
                writable=False,
            ),
            Capability(
                name="energy.home.power",
                kind=CapabilityKind.NUMBER,
                unit="W",
                readable=True,
                writable=False,
            ),
            Capability(
                name="battery.soc",
                kind=CapabilityKind.NUMBER,
                unit="%",
                readable=True,
                writable=False,
            ),
            Capability(
                name="battery.capacity",
                kind=CapabilityKind.NUMBER,
                unit="kWh",
                readable=True,
                writable=False,
            ),
        ],
        metadata={"mapping_mode": "explicit_entity_capability"},
    )


class HomeAssistantProvider:
    """Translate HA states, registry metadata and services into Provider SDK v1.

    ``external_device_id`` is an entity ID for commands. Discovery groups
    entities into one descriptor when HA supplies a shared ``device_id``;
    names, areas and manufacturers never create an inferred physical identity.
    """

    manifest = _manifest()

    def __init__(
        self,
        client: HomeAssistantClient,
        *,
        metric_mappings: Mapping[str, Mapping[str, str]] | None = None,
        battery_capacity_bindings: Mapping[str, HomeAssistantBatteryCapacityBinding] | None = None,
        battery_dispatch_bindings: Mapping[str, HomeAssistantDispatchableBatteryBinding]
        | None = None,
        ev_charging_bindings: Mapping[str, HomeAssistantEVChargingBinding] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.client = client
        self._clock = clock or SystemClock()
        self.mapper = HomeAssistantMapper()
        self.metric_mappings = {
            str(entity_id): {str(capability): str(metric) for capability, metric in mapping.items()}
            for entity_id, mapping in (metric_mappings or {}).items()
        }
        self.battery_capacity_bindings = {
            str(entity_id): HomeAssistantBatteryCapacityBinding.model_validate(binding)
            for entity_id, binding in (battery_capacity_bindings or {}).items()
        }
        self.battery_dispatch_bindings = {
            str(binding_id): HomeAssistantDispatchableBatteryBinding.model_validate(binding)
            for binding_id, binding in (battery_dispatch_bindings or {}).items()
        }
        self.ev_charging_bindings = {
            str(binding_id): HomeAssistantEVChargingBinding.model_validate(binding)
            for binding_id, binding in (ev_charging_bindings or {}).items()
        }
        self._entities: dict[str, dict[str, Any]] = {}
        self._entity_device: dict[str, str] = {}
        self._entity_commands: dict[str, set[str]] = {}
        self._resolved_capacity_device_ids: dict[str, str] = {}
        self._resolved_dispatch_device_ids: dict[str, str] = {}
        self._resolved_ev_device_ids: dict[str, str] = {}
        # Best-effort, process-local duplicate suppression only -- reset on
        # restart, not shared across processes. The authoritative barrier
        # against re-executing a command is the persistent execution claim
        # in PlanRepository.claim_for_execution (Spec 057).
        self._executed_idempotency_keys: set[str] = set()
        self._connected = False

    async def connect(self) -> None:
        if not await self.client.health():
            raise ConnectionError("Home Assistant is unavailable")
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def discover(self) -> list[DeviceDescriptor]:
        snapshot = await self._load_snapshot()
        descriptors: dict[str, DeviceDescriptor] = {}
        for entity in snapshot.source_entities:
            entity_id = str(entity["entity_id"])
            source_device_id = str(entity.get("device_id") or entity_id)
            capabilities = [
                Capability.model_validate(item) for item in entity.get("capabilities", [])
            ]
            existing = descriptors.get(source_device_id)
            if existing is None:
                raw = self._entities.get(entity_id, {})
                descriptors[source_device_id] = DeviceDescriptor(
                    provider_id=self.manifest.provider_id,
                    external_id=source_device_id,
                    device_type=DeviceType(str(entity["semantic_type"])),
                    name=str(entity.get("name") or entity_id),
                    manufacturer=_first_text(entity.get("manufacturer"), raw.get("manufacturer")),
                    model=_first_text(entity.get("model"), raw.get("model")),
                    serial_number=_first_text(
                        entity.get("serial_number"), raw.get("serial_number")
                    ),
                    area_id=_optional_text(entity.get("area_id")),
                    capabilities=capabilities,
                    identity_keys=_unique_strings(entity.get("identity_keys", [])),
                    connections=_unique_strings([entity_id, *entity.get("connections", [])]),
                    parent_external_id=_optional_text(entity.get("parent_source_device_id")),
                )
                continue

            descriptors[source_device_id] = existing.model_copy(
                update={
                    "capabilities": _merge_capabilities(existing.capabilities, capabilities),
                    "connections": _unique_strings(
                        [*existing.connections, entity_id, *entity.get("connections", [])]
                    ),
                    "identity_keys": _unique_strings(
                        [*existing.identity_keys, *entity.get("identity_keys", [])]
                    ),
                }
            )
        return list(descriptors.values())

    async def snapshot(self) -> AdapterSnapshot:
        """Return the normalized source snapshot used by the runtime bridge."""

        return await self._load_snapshot()

    async def get_measurements(self, device_ids: Sequence[str] | None = None) -> list[Measurement]:
        snapshot = await self._load_snapshot()
        allowed = set(device_ids or ())
        return self._measurements_for_snapshot(snapshot.source_states, allowed or None)

    async def execute(
        self, command: ProviderCommand, execution_context: ExecutionContext | None = None
    ) -> ProviderExecutionResult:
        entity_id = command.external_device_id
        source = SourceRef(adapter_id=self.manifest.provider_id, external_id=entity_id)
        if entity_id not in self._entities:
            return self._result(command, ExecutionStatus.REJECTED, "Unknown Home Assistant entity")
        if command.idempotency_key in self._executed_idempotency_keys:
            return self._result(command, ExecutionStatus.REJECTED, "Duplicate idempotency key")
        if command.command not in self._entity_commands.get(entity_id, set()):
            return self._result(
                command, ExecutionStatus.REJECTED, "Unsupported Home Assistant command"
            )

        translated = self._translate_command(entity_id, command)
        if translated is None:
            if (
                self._battery_route(entity_id, command.command) is not None
                and command.params.get("value") is not None
            ):
                return self._result(
                    command,
                    ExecutionStatus.REJECTED,
                    "numeric battery command value cannot be represented by this route",
                )
            return self._result(command, ExecutionStatus.REJECTED, "Invalid Home Assistant command")
        domain, service, data = translated
        try:
            if execution_context is None:
                await self.client.call_service(domain, service, data)
            else:
                await self.client.call_service(
                    domain, service, data, execution_context=execution_context
                )
        except Exception:
            return self._result(
                command, ExecutionStatus.FAILED, "Home Assistant service call failed"
            )
        self._executed_idempotency_keys.add(command.idempotency_key)
        return self._result(
            command,
            ExecutionStatus.CONFIRMED_SUCCESS,
            "Home Assistant service call accepted",
            source,
        )

    async def subscribe(self) -> AsyncIterator[Measurement]:
        async for message in self.client.subscribe_state_events():
            entity_id, new_state = _state_changed_payload(message)
            if entity_id is None or new_state is None:
                continue
            entity = deepcopy(self._entities.get(entity_id, {"entity_id": entity_id}))
            entity.update(new_state)
            entity = self._apply_configured_canonical_ids([entity])[0]
            entity = self._normalize_entity(entity)
            self._entities[entity_id] = entity
            normalized = [entity]
            snapshot = self.mapper.to_snapshot(normalized)
            snapshot = self._project_battery_control_capabilities(snapshot)
            snapshot = self._project_ev_charging_capabilities(snapshot)
            for measurement in self._measurements_for_snapshot(snapshot.source_states, None):
                yield measurement

    async def _load_snapshot(self) -> AdapterSnapshot:
        entities = await self.client.fetch_states()
        entities = await self._merge_entity_registry(entities)
        normalized = [self._normalize_entity(entity) for entity in entities]
        normalized = self._apply_configured_canonical_ids(normalized)
        snapshot = self.mapper.to_snapshot(normalized)
        self._remember_routes(normalized, snapshot.source_entities)
        snapshot = self._project_battery_control_capabilities(snapshot)
        snapshot = self._project_ev_charging_capabilities(snapshot)
        self._remember_routes(normalized, snapshot.source_entities)
        self._resolve_configured_device_ids(snapshot.source_entities)
        self._validate_capacity_bindings()
        return snapshot

    def _project_battery_control_capabilities(self, snapshot: AdapterSnapshot) -> AdapterSnapshot:
        """Expose configured battery commands without fabricating telemetry."""

        entities = [deepcopy(entity) for entity in snapshot.source_entities]
        by_entity_id = {str(entity["entity_id"]): entity for entity in entities}
        for binding in self.battery_dispatch_bindings.values():
            for route in (binding.charge, binding.discharge, binding.stop):
                entity = by_entity_id.get(route.entity_id)
                if entity is None:
                    continue
                capabilities = [
                    Capability.model_validate(item) for item in entity.get("capabilities", [])
                ]
                existing = next(
                    (
                        capability
                        for capability in capabilities
                        if capability.name == binding.control_capability
                    ),
                    None,
                )
                if existing is None:
                    capabilities.append(
                        self._battery_control_capability(route, binding.control_capability)
                    )
                elif route.provider_command not in existing.commands:
                    capabilities = [
                        capability.model_copy(
                            update={"commands": [*capability.commands, route.provider_command]}
                        )
                        if capability.name == binding.control_capability
                        else capability
                        for capability in capabilities
                    ]
                entity["capabilities"] = [capability.model_dump() for capability in capabilities]
        return snapshot.model_copy(update={"source_entities": entities})

    def _project_ev_charging_capabilities(self, snapshot: AdapterSnapshot) -> AdapterSnapshot:
        """Expose only explicitly bound EV telemetry and command routes.

        Home Assistant's generic mapper intentionally does not assign runtime
        semantics to ``binary_sensor`` and ``number`` entities.  An EV route
        binding is the server-owned declaration that supplies those semantics;
        this projection keeps the raw source timestamps and never invents a
        value when the source did not publish one.
        """

        entities = [deepcopy(entity) for entity in snapshot.source_entities]
        source_states = [dict(state) for state in snapshot.source_states]
        by_entity_id = {str(entity["entity_id"]): entity for entity in entities}
        state_keys = {
            (str(state["entity_id"]), str(state["capability"])) for state in source_states
        }
        for binding in self.ev_charging_bindings.values():
            telemetry = (
                (binding.soc_entity_id, binding.soc_unit, CapabilityKind.NUMBER),
                (
                    binding.power_feedback_entity_id,
                    binding.power_unit,
                    CapabilityKind.NUMBER,
                ),
                (binding.capacity_entity_id, binding.capacity_unit, CapabilityKind.NUMBER),
                (binding.connected_entity_id, None, CapabilityKind.BOOLEAN),
            )
            for entity_id, unit, kind in telemetry:
                entity = by_entity_id.get(entity_id)
                if entity is None:
                    continue
                capabilities = [
                    Capability.model_validate(item) for item in entity.get("capabilities", [])
                ]
                if not any(capability.name == "value" for capability in capabilities):
                    capabilities.append(
                        Capability(
                            name="value",
                            kind=kind,
                            unit=unit,
                            readable=True,
                            writable=False,
                        )
                    )
                    entity["capabilities"] = [
                        capability.model_dump() for capability in capabilities
                    ]
                if (entity_id, "value") not in state_keys:
                    raw = self._entities.get(entity_id)
                    if raw is None:
                        continue
                    raw_state = raw.get("state", {})
                    value = raw_state.get("value") if isinstance(raw_state, dict) else None
                    if kind is CapabilityKind.BOOLEAN and value is not None:
                        value = _on_state(value)
                    source_states.append(
                        {
                            "entity_id": entity_id,
                            "capability": "value",
                            "value": value,
                            "unit": unit,
                            "available": raw.get("available", value is not None),
                            "observed_at": raw.get("last_updated")
                            or raw.get("last_changed"),
                            "received_at": raw.get("received_at"),
                        }
                    )
                    state_keys.add((entity_id, "value"))

            commands = [binding.charge.provider_command, binding.stop.provider_command]
            for route in (binding.charge, binding.stop):
                command_entity = by_entity_id.get(route.entity_id)
                if command_entity is None:
                    continue
                capabilities = [
                    Capability.model_validate(item)
                    for item in command_entity.get("capabilities", [])
                ]
                existing = next(
                    (
                        capability
                        for capability in capabilities
                        if capability.name == "ev_charging"
                    ),
                    None,
                )
                if existing is None:
                    capabilities.append(self._ev_control_capability(route, commands))
                else:
                    capabilities = [
                        capability.model_copy(
                            update={
                                "commands": list(
                                    dict.fromkeys([*capability.commands, *commands])
                                )
                            }
                        )
                        if capability.name == "ev_charging"
                        else capability
                        for capability in capabilities
                    ]
                command_entity["capabilities"] = [
                    capability.model_dump() for capability in capabilities
                ]
        for binding in self.ev_charging_bindings.values():
            for state in source_states:
                if (
                    str(state.get("entity_id")) == binding.connected_entity_id
                    and state.get("value") is not None
                ):
                    state["value"] = _on_state(state["value"])
        return snapshot.model_copy(
            update={"source_entities": entities, "source_states": source_states}
        )

    def _ev_control_capability(
        self, route: HomeAssistantBatteryCommandRoute, commands: list[str]
    ) -> Capability:
        minimum, maximum = self._numeric_route_bounds(route)
        return Capability(
            name="ev_charging",
            kind=CapabilityKind.NUMBER,
            unit="kW",
            readable=False,
            writable=True,
            minimum=minimum,
            maximum=maximum,
            commands=commands,
        )

    def _apply_configured_canonical_ids(
        self, entities: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Project only explicit mapping identities onto configured battery routes.

        A Home Assistant entity/device ID is provider-local.  The canonical ID
        is therefore supplied by the server-owned mapping and copied to every
        route entity of that binding.  This method deliberately does not
        create a dispatch binding or grant actuator authority.
        """

        projected = [deepcopy(entity) for entity in entities]
        by_entity_id = {str(entity["entity_id"]): entity for entity in projected}
        assigned: dict[str, str] = {}
        for binding in self.battery_dispatch_bindings.values():
            canonical_device_id = binding.canonical_device_id
            if canonical_device_id is None:
                continue
            entity_ids = (
                binding.soc_entity_id,
                binding.power_feedback_entity_id,
                binding.capacity_entity_id,
                binding.charge.entity_id,
                binding.discharge.entity_id,
                binding.stop.entity_id,
            )
            for entity_id in dict.fromkeys(entity_ids):
                entity = by_entity_id.get(entity_id)
                if entity is None:
                    continue
                previous = assigned.get(entity_id)
                if previous is not None and previous != canonical_device_id:
                    raise HomeAssistantMappingConfigurationError(
                        "Home Assistant entity is assigned to multiple canonical devices"
                    )
                assigned[entity_id] = canonical_device_id
                entity["canonical_id"] = canonical_device_id
        for ev_binding in self.ev_charging_bindings.values():
            canonical_device_id = ev_binding.canonical_device_id
            if canonical_device_id is None:
                continue
            entity_ids = (
                ev_binding.soc_entity_id,
                ev_binding.power_feedback_entity_id,
                ev_binding.capacity_entity_id,
                ev_binding.connected_entity_id,
                ev_binding.charge.entity_id,
                ev_binding.stop.entity_id,
            )
            for entity_id in dict.fromkeys(entity_ids):
                entity = by_entity_id.get(entity_id)
                if entity is None:
                    continue
                previous = assigned.get(entity_id)
                if previous is not None and previous != canonical_device_id:
                    raise HomeAssistantMappingConfigurationError(
                        "Home Assistant entity is assigned to multiple canonical devices"
                    )
                assigned[entity_id] = canonical_device_id
                entity["canonical_id"] = canonical_device_id
        return projected

    def _battery_control_capability(
        self, route: HomeAssistantBatteryCommandRoute, name: str
    ) -> Capability:
        minimum, maximum = self._numeric_route_bounds(route)
        return Capability(
            name=name,
            kind=CapabilityKind.NUMBER,
            unit="kW",
            readable=False,
            writable=True,
            minimum=minimum,
            maximum=maximum,
            commands=[route.provider_command],
        )

    def _numeric_route_bounds(
        self, route: HomeAssistantBatteryCommandRoute
    ) -> tuple[float | None, float | None]:
        if route.service != "set_value":
            return None, None
        raw = self._entities.get(route.entity_id, {})
        attributes = raw.get("attributes", {})
        if not isinstance(attributes, dict):
            return None, None
        minimum = attributes.get("min")
        maximum = attributes.get("max")
        if (
            not isinstance(minimum, (int, float))
            or isinstance(minimum, bool)
            or not isinstance(maximum, (int, float))
            or isinstance(maximum, bool)
            or not math.isfinite(float(minimum))
            or not math.isfinite(float(maximum))
        ):
            return None, None
        magnitude = max(abs(float(minimum)), abs(float(maximum)))
        return 0.0, magnitude

    async def _merge_entity_registry(self, entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not entities:
            return entities
        fetch_registry = getattr(self.client, "fetch_entity_registry", None)
        if not callable(fetch_registry):
            return entities
        try:
            registry_entries = await fetch_registry()
        except Exception:
            return entities
        fetch_devices = getattr(self.client, "fetch_device_registry", None)
        try:
            device_entries = await fetch_devices() if callable(fetch_devices) else []
        except Exception:
            device_entries = []
        registry = {
            str(entry["entity_id"]): entry
            for entry in registry_entries
            if isinstance(entry, dict) and entry.get("entity_id")
        }
        devices = {
            str(entry["id"]): entry
            for entry in device_entries
            if isinstance(entry, dict) and entry.get("id")
        }
        merged: list[dict[str, Any]] = []
        for entity in entities:
            item = deepcopy(entity)
            item.update(
                {
                    key: value
                    for key, value in registry.get(str(item["entity_id"]), {}).items()
                    if value is not None
                }
            )
            device = devices.get(str(item.get("device_id")))
            if device is not None:
                item["identity_keys"] = _registry_values(device.get("identifiers", []))
                item["connections"] = _registry_values(device.get("connections", []))
            merged.append(item)
        return merged

    def _remember_routes(
        self, normalized: list[dict[str, Any]], source_entities: list[dict[str, Any]]
    ) -> None:
        self._entities = {str(entity["entity_id"]): deepcopy(entity) for entity in normalized}
        self._entity_device = {
            str(entity["entity_id"]): str(entity.get("device_id") or entity["entity_id"])
            for entity in source_entities
        }
        self._entity_commands = {
            str(entity["entity_id"]): {
                str(command)
                for capability in entity.get("capabilities", [])
                for command in capability.get("commands", [])
            }
            for entity in source_entities
        }

    def _resolve_configured_device_ids(self, source_entities: list[dict[str, Any]]) -> None:
        self._resolved_capacity_device_ids.clear()
        self._resolved_dispatch_device_ids.clear()
        self._resolved_ev_device_ids.clear()
        for entity_id, binding in self.battery_capacity_bindings.items():
            self._resolved_capacity_device_ids[entity_id] = self._resolve_binding_device_id(
                source_entities=source_entities,
                entity_ids=[entity_id],
                configured_device_id=binding.device_id,
                identity_claims=binding.identity_claims,
                label="capacity",
            )

    def _resolve_dispatch_device_ids(self, source_entities: list[dict[str, Any]]) -> None:
        self._resolved_dispatch_device_ids.clear()
        for binding_id, binding in self.battery_dispatch_bindings.items():
            entity_ids = [
                binding.soc_entity_id,
                binding.power_feedback_entity_id,
                binding.capacity_entity_id,
                binding.charge.entity_id,
                binding.discharge.entity_id,
                binding.stop.entity_id,
            ]
            resolved_device_id = self._resolve_binding_device_id(
                source_entities=source_entities,
                entity_ids=list(dict.fromkeys(entity_ids)),
                configured_device_id=binding.device_id,
                identity_claims=binding.identity_claims,
                label="dispatch",
            )
            self._resolved_dispatch_device_ids[binding_id] = resolved_device_id
            capacity_device_id = self._resolved_capacity_device_ids.get(binding.capacity_entity_id)
            if capacity_device_id != resolved_device_id:
                raise HomeAssistantMappingConfigurationError(
                "Home Assistant battery capacity and dispatch identities "
                "resolve to different devices"
            )

    def _resolve_ev_device_ids(self, source_entities: list[dict[str, Any]]) -> None:
        self._resolved_ev_device_ids.clear()
        for binding_id, binding in self.ev_charging_bindings.items():
            entity_ids = [
                binding.soc_entity_id,
                binding.power_feedback_entity_id,
                binding.capacity_entity_id,
                binding.connected_entity_id,
                binding.charge.entity_id,
                binding.stop.entity_id,
            ]
            self._resolved_ev_device_ids[binding_id] = self._resolve_binding_device_id(
                source_entities=source_entities,
                entity_ids=list(dict.fromkeys(entity_ids)),
                configured_device_id=binding.device_id,
                identity_claims=binding.identity_claims,
                label="EV charging",
            )

    def _resolve_binding_device_id(
        self,
        *,
        source_entities: list[dict[str, Any]],
        entity_ids: list[str],
        configured_device_id: str | None,
        identity_claims: HomeAssistantIdentityClaims | None,
        label: str,
    ) -> str:
        by_entity = {str(entity["entity_id"]): entity for entity in source_entities}
        if identity_claims is None:
            if configured_device_id is None:
                raise HomeAssistantMappingConfigurationError(
                    f"Home Assistant {label} binding has no device identity"
                )
            for entity_id in entity_ids:
                entity = by_entity.get(entity_id)
                if entity is None:
                    continue
                actual_device_id = str(entity.get("device_id") or entity_id)
                if actual_device_id != configured_device_id:
                    raise HomeAssistantMappingConfigurationError(
                        f"Home Assistant {label} binding device does not match entity registry"
                    )
            return configured_device_id

        candidates = {
            str(entity.get("device_id") or entity.get("entity_id"))
            for entity in source_entities
            if self._entity_matches_identity(entity, identity_claims)
        }
        if not candidates:
            raise HomeAssistantMappingConfigurationError(
                f"Home Assistant {label} identity claims were not found"
            )
        if len(candidates) != 1:
            raise HomeAssistantMappingConfigurationError(
                f"Home Assistant {label} identity claims are ambiguous"
            )
        resolved_device_id = next(iter(candidates))
        for entity_id in entity_ids:
            entity = by_entity.get(entity_id)
            if entity is None:
                continue
            actual_device_id = str(entity.get("device_id") or entity_id)
            if actual_device_id != resolved_device_id:
                raise HomeAssistantMappingConfigurationError(
                    f"Home Assistant {label} entity belongs to a different resolved device"
                )
        return resolved_device_id

    @staticmethod
    def _entity_matches_identity(
        entity: dict[str, Any], identity_claims: HomeAssistantIdentityClaims
    ) -> bool:
        identity_keys = {str(item) for item in entity.get("identity_keys", [])}
        connections = {str(item) for item in entity.get("connections", [])}
        return set(identity_claims.identity_keys).issubset(identity_keys) and set(
            identity_claims.connections
        ).issubset(connections)

    def _validate_capacity_bindings(self) -> None:
        for entity_id in self.battery_capacity_bindings:
            if entity_id not in self._resolved_capacity_device_ids:
                raise HomeAssistantMappingConfigurationError(
                    "Home Assistant capacity binding device could not be resolved"
                )

    def validate_battery_dispatch_routes(self, snapshot: AdapterSnapshot) -> None:
        """Validate configured battery routes against an existing snapshot.

        The caller owns snapshot acquisition. This method is deliberately
        synchronous and side-effect free: it never refreshes Home Assistant,
        calls a service or constructs a canonical energy provider.
        """

        if not self.battery_dispatch_bindings:
            return

        self._resolve_dispatch_device_ids(snapshot.source_entities)

        source_entities = {str(entity["entity_id"]): entity for entity in snapshot.source_entities}
        source_states = [state for state in snapshot.source_states if isinstance(state, dict)]
        for binding_id, binding in self.battery_dispatch_bindings.items():
            resolved_device_id = self._resolved_dispatch_device_ids.get(binding_id)
            if resolved_device_id is None:
                raise HomeAssistantMappingConfigurationError(
                    "Home Assistant battery dispatch binding device could not be resolved"
                )
            telemetry_entities = {
                binding.soc_entity_id,
                binding.power_feedback_entity_id,
                binding.capacity_entity_id,
            }
            if len(telemetry_entities) != 3:
                raise HomeAssistantMappingConfigurationError(
                    "Home Assistant battery telemetry entities must be distinct"
                )

            if binding.capacity_entity_id not in self.battery_capacity_bindings:
                raise HomeAssistantMappingConfigurationError(
                    "Home Assistant battery capacity binding is missing"
                )

            all_routes = [
                binding.soc_entity_id,
                binding.power_feedback_entity_id,
                binding.capacity_entity_id,
                binding.charge.entity_id,
                binding.discharge.entity_id,
                binding.stop.entity_id,
            ]
            for entity_id in set(all_routes):
                entity = source_entities.get(entity_id)
                if entity is None:
                    raise HomeAssistantMappingConfigurationError(
                        "Home Assistant battery dispatch source entity is missing"
                    )
                actual_device_id = str(entity.get("device_id") or entity_id)
                if actual_device_id != resolved_device_id:
                    raise HomeAssistantMappingConfigurationError(
                        "Home Assistant battery dispatch source device does not match"
                    )
                if not entity.get("available", True):
                    raise HomeAssistantMappingConfigurationError(
                        "Home Assistant battery dispatch source entity is unavailable"
                    )

            self._validate_battery_telemetry_route(
                source_entities,
                source_states,
                entity_id=binding.soc_entity_id,
                expected_metric=binding.soc_capability,
                expected_unit=binding.soc_unit,
                label="SOC",
            )
            self._validate_battery_telemetry_route(
                source_entities,
                source_states,
                entity_id=binding.power_feedback_entity_id,
                expected_metric=binding.power_feedback_capability,
                expected_unit=binding.power_unit,
                label="power feedback",
            )
            self._validate_battery_capacity_route(
                source_entities,
                source_states,
                resolved_device_id,
                binding.capacity_entity_id,
            )
            for route in (binding.charge, binding.discharge, binding.stop):
                self._validate_battery_command_route(source_entities, route)

    def validate_ev_charging_routes(self, snapshot: AdapterSnapshot) -> None:
        """Validate configured EV telemetry and command routes read-only."""

        if not self.ev_charging_bindings:
            return
        self._resolve_ev_device_ids(snapshot.source_entities)
        source_entities = {str(entity["entity_id"]): entity for entity in snapshot.source_entities}
        source_states = [state for state in snapshot.source_states if isinstance(state, dict)]
        for binding_id, binding in self.ev_charging_bindings.items():
            resolved_device_id = self._resolved_ev_device_ids.get(binding_id)
            if resolved_device_id is None:
                raise HomeAssistantMappingConfigurationError(
                    "Home Assistant EV charging binding device could not be resolved"
                )
            all_routes = [
                binding.soc_entity_id,
                binding.power_feedback_entity_id,
                binding.capacity_entity_id,
                binding.connected_entity_id,
                binding.charge.entity_id,
                binding.stop.entity_id,
            ]
            for entity_id in set(all_routes):
                entity = source_entities.get(entity_id)
                if entity is None:
                    raise HomeAssistantMappingConfigurationError(
                        "Home Assistant EV charging source entity is missing"
                    )
                actual_device_id = str(entity.get("device_id") or entity_id)
                if actual_device_id != resolved_device_id:
                    raise HomeAssistantMappingConfigurationError(
                        "Home Assistant EV charging source device does not match"
                    )
                if not entity.get("available", True):
                    raise HomeAssistantMappingConfigurationError(
                        "Home Assistant EV charging source entity is unavailable: "
                        f"{entity_id}"
                    )

            self._validate_ev_telemetry_route(
                source_entities,
                source_states,
                entity_id=binding.soc_entity_id,
                expected_metric=binding.soc_capability,
                expected_unit=binding.soc_unit,
                label="SOC",
            )
            self._validate_ev_telemetry_route(
                source_entities,
                source_states,
                entity_id=binding.power_feedback_entity_id,
                expected_metric=binding.power_feedback_capability,
                expected_unit=binding.power_unit,
                label="power feedback",
            )
            self._validate_ev_telemetry_route(
                source_entities,
                source_states,
                entity_id=binding.capacity_entity_id,
                expected_metric=binding.capacity_metric,
                expected_unit=binding.capacity_unit,
                label="capacity",
            )
            self._validate_ev_connected_route(source_entities, source_states, binding)
            for route in (binding.charge, binding.stop):
                self._validate_battery_command_route(source_entities, route)

    def _validate_ev_telemetry_route(
        self,
        source_entities: Mapping[str, dict[str, Any]],
        source_states: list[dict[str, Any]],
        *,
        entity_id: str,
        expected_metric: str,
        expected_unit: str,
        label: str,
    ) -> None:
        entity = source_entities[entity_id]
        capabilities = [Capability.model_validate(item) for item in entity.get("capabilities", [])]
        matching_states = [
            state
            for state in source_states
            if str(state.get("entity_id")) == entity_id
            and self.semantic_capability(entity_id, str(state.get("capability")))
            == expected_metric
        ]
        matching_capabilities = [
            capability
            for capability in capabilities
            if capability.name in {str(state.get("capability")) for state in matching_states}
        ]
        if not matching_capabilities or not any(
            capability.readable
            and capability.kind is CapabilityKind.NUMBER
            and capability.unit == expected_unit
            for capability in matching_capabilities
        ):
            raise HomeAssistantMappingConfigurationError(
                f"Home Assistant EV charging {label} capability is missing or incompatible"
            )
        if not any(
            state.get("available", True)
            and state.get("unit") == expected_unit
            and _finite_number(state.get("value"))
            for state in matching_states
        ):
            raise HomeAssistantMappingConfigurationError(
                f"Home Assistant EV charging {label} state is unavailable or invalid"
            )

    def _validate_ev_connected_route(
        self,
        source_entities: Mapping[str, dict[str, Any]],
        source_states: list[dict[str, Any]],
        binding: HomeAssistantEVChargingBinding,
    ) -> None:
        entity = source_entities[binding.connected_entity_id]
        capabilities = [Capability.model_validate(item) for item in entity.get("capabilities", [])]
        matching_states = [
            state
            for state in source_states
            if str(state.get("entity_id")) == binding.connected_entity_id
            and self.semantic_capability(
                binding.connected_entity_id, str(state.get("capability"))
            ) == binding.connected_capability
        ]
        if not any(
            capability.readable
            and capability.kind is CapabilityKind.BOOLEAN
            and capability.unit is None
            for capability in capabilities
            if capability.name in {str(state.get("capability")) for state in matching_states}
        ) or not any(
            state.get("available", True) and isinstance(state.get("value"), bool)
            for state in matching_states
        ):
            raise HomeAssistantMappingConfigurationError(
                "Home Assistant EV charging connected capability is missing or unavailable"
            )

    def _validate_battery_telemetry_route(
        self,
        source_entities: Mapping[str, dict[str, Any]],
        source_states: list[dict[str, Any]],
        *,
        entity_id: str,
        expected_metric: str,
        expected_unit: str,
        label: str,
    ) -> None:
        entity = source_entities[entity_id]
        capabilities = [Capability.model_validate(item) for item in entity.get("capabilities", [])]
        matching_states = [
            state
            for state in source_states
            if str(state.get("entity_id")) == entity_id
            and self.semantic_capability(entity_id, str(state.get("capability")))
            == expected_metric
        ]
        matching_capabilities = [
            capability
            for capability in capabilities
            if capability.name in {str(state.get("capability")) for state in matching_states}
        ]
        if not matching_capabilities or not any(
            capability.readable
            and capability.kind is CapabilityKind.NUMBER
            and capability.unit == expected_unit
            for capability in matching_capabilities
        ):
            raise HomeAssistantMappingConfigurationError(
                f"Home Assistant battery {label} capability is missing or incompatible"
            )
        if not any(
            state.get("available", True)
            and state.get("unit") == expected_unit
            and _finite_number(state.get("value"))
            for state in matching_states
        ):
            raise HomeAssistantMappingConfigurationError(
                f"Home Assistant battery {label} state is unavailable or invalid"
            )

    def _validate_battery_capacity_route(
        self,
        source_entities: Mapping[str, dict[str, Any]],
        source_states: list[dict[str, Any]],
        device_id: str,
        entity_id: str,
    ) -> None:
        resolved_device_id = self._resolved_capacity_device_ids.get(entity_id)
        if resolved_device_id != device_id:
            raise HomeAssistantMappingConfigurationError(
                "Home Assistant battery capacity binding device does not match"
            )
        raw_entity = self._entities.get(entity_id, {})
        attributes = raw_entity.get("attributes", {})
        if not isinstance(attributes, dict) or attributes.get("device_class") != ("energy_storage"):
            raise HomeAssistantMappingConfigurationError(
                "Home Assistant battery capacity requires energy_storage device class"
            )
        entity = source_entities[entity_id]
        capabilities = [Capability.model_validate(item) for item in entity.get("capabilities", [])]
        capacity_states = [
            state for state in source_states if str(state.get("entity_id")) == entity_id
        ]
        if not any(
            capability.readable
            and capability.kind is CapabilityKind.NUMBER
            and capability.unit == "kWh"
            for capability in capabilities
        ) or not any(
            state.get("available", True)
            and state.get("unit") == "kWh"
            and _finite_number(state.get("value"))
            for state in capacity_states
        ):
            raise HomeAssistantMappingConfigurationError(
                "Home Assistant battery capacity state is unavailable or invalid"
            )

    def _validate_battery_command_route(
        self,
        source_entities: Mapping[str, dict[str, Any]],
        route: HomeAssistantBatteryCommandRoute,
    ) -> None:
        entity = source_entities[route.entity_id]
        capabilities = [Capability.model_validate(item) for item in entity.get("capabilities", [])]
        configured_capability = next(
            (
                capability
                for capability in capabilities
                if capability.writable and route.provider_command in capability.commands
            ),
            None,
        )
        if configured_capability is None:
            raise HomeAssistantMappingConfigurationError(
                "Home Assistant battery command route is unsupported or not writable"
            )
        if route.service == "set_value":
            if route.entity_id.split(".", 1)[0] != route.service_domain:
                raise HomeAssistantMappingConfigurationError(
                    "Home Assistant numeric battery command entity must use the number domain"
                )
            raw = self._entities.get(route.entity_id, {})
            attributes = raw.get("attributes", {})
            if not isinstance(attributes, dict) or attributes.get("unit_of_measurement") != "kW":
                raise HomeAssistantMappingConfigurationError(
                    "Home Assistant numeric battery command must use kW"
                )
            if not _finite_number(attributes.get("min")) or not _finite_number(
                attributes.get("max")
            ):
                raise HomeAssistantMappingConfigurationError(
                    "Home Assistant numeric battery command must expose finite bounds"
                )
            minimum = float(attributes["min"])
            maximum = float(attributes["max"])
            if minimum > maximum:
                raise HomeAssistantMappingConfigurationError(
                    "Home Assistant numeric battery command bounds are invalid"
                )
            if route.value_transform == "negate" and minimum >= 0:
                raise HomeAssistantMappingConfigurationError(
                    "Home Assistant discharge command must accept negative values"
                )
            if route.value_transform == "as_is" and maximum <= 0:
                raise HomeAssistantMappingConfigurationError(
                    "Home Assistant charge command must accept positive values"
                )
            if route.value_transform == "zero" and (minimum > 0 or maximum < 0):
                raise HomeAssistantMappingConfigurationError(
                    "Home Assistant numeric battery command must accept zero"
                )
        candidate = ProviderCommand(
            provider_id=self.manifest.provider_id,
            external_device_id=route.entity_id,
            command=route.provider_command,
            params=({"value": 0} if route.value_transform in {"as_is", "negate"} else {}),
            idempotency_key=(f"route-validation:{route.entity_id}:{route.provider_command}"),
        )
        if self._translate_command(route.entity_id, candidate) is None:
            raise HomeAssistantMappingConfigurationError(
                "Home Assistant battery command route cannot be translated"
            )

    def _measurements_for_snapshot(
        self, source_states: list[dict[str, Any]], allowed_devices: set[str] | None
    ) -> list[Measurement]:
        received_at = self._clock.now()
        measurements: list[Measurement] = []
        for state in source_states:
            entity_id = str(state["entity_id"])
            device_id = self._entity_device.get(entity_id, entity_id)
            if (
                allowed_devices is not None
                and entity_id not in allowed_devices
                and device_id not in allowed_devices
            ):
                continue
            raw = self._entities.get(entity_id, {})
            capacity_binding = self.battery_capacity_bindings.get(entity_id)
            metric: str | None
            if capacity_binding is not None:
                resolved_device_id = self._resolved_capacity_device_ids.get(entity_id)
                if resolved_device_id != device_id:
                    raise HomeAssistantMappingConfigurationError(
                        "Home Assistant capacity binding device does not match entity state"
                    )
                attributes = raw.get("attributes", {})
                if not isinstance(attributes, dict) or attributes.get("device_class") != (
                    "energy_storage"
                ):
                    raise HomeAssistantMappingConfigurationError(
                        "Home Assistant capacity binding requires device_class energy_storage"
                    )
                metric = "battery.capacity"
            else:
                metric = self.semantic_capability(entity_id, str(state["capability"]))
            if metric is None:
                continue
            observed_at = _timestamp(
                raw.get("last_updated"), raw.get("last_changed"), fallback=received_at
            )
            measurement_received_at = max(
                observed_at,
                _timestamp(raw.get("received_at"), fallback=received_at),
            )
            value = _scalar(state.get("value"))
            quality = (
                MeasurementQuality.GOOD
                if state.get("available", True) and value is not None
                else MeasurementQuality.UNAVAILABLE
            )
            measurements.append(
                Measurement(
                    provider_id=self.manifest.provider_id,
                    device_id=device_id,
                    metric=metric,
                    value=value if value is not None else "unavailable",
                    unit=state.get("unit"),
                    observed_at=observed_at,
                    received_at=measurement_received_at,
                    quality=quality,
                    source_ref=SourceRef(
                        adapter_id=self.manifest.provider_id,
                        external_id=entity_id,
                    ),
                    nominal_capacity_attestation=(
                        capacity_binding.nominal_capacity_attestation
                        if capacity_binding is not None
                        else None
                    ),
                )
            )
        return measurements

    def _metric_for(self, entity_id: str, capability: str) -> str | None:
        explicit = self.metric_mappings.get(entity_id, {}).get(capability)
        if explicit:
            return explicit
        if entity_id.split(".", 1)[0] == "sensor":
            return None
        return capability

    def semantic_capability(self, entity_id: str, capability: str) -> str | None:
        """Return the configured runtime capability for one source field."""

        if entity_id in self.battery_capacity_bindings:
            return "battery.capacity"
        for ev_binding in self.ev_charging_bindings.values():
            if entity_id == ev_binding.soc_entity_id:
                return ev_binding.soc_capability
            if entity_id == ev_binding.power_feedback_entity_id:
                return ev_binding.power_feedback_capability
            if entity_id == ev_binding.capacity_entity_id:
                return ev_binding.capacity_metric
            if entity_id == ev_binding.connected_entity_id:
                return ev_binding.connected_capability
        for battery_binding in self.battery_dispatch_bindings.values():
            if entity_id == battery_binding.soc_entity_id:
                return battery_binding.soc_capability
            if entity_id == battery_binding.power_feedback_entity_id:
                return battery_binding.power_feedback_capability
            if entity_id == battery_binding.capacity_entity_id:
                return battery_binding.capacity_metric
        return self._metric_for(entity_id, capability)

    @staticmethod
    def _normalize_entity(entity: dict[str, Any]) -> dict[str, Any]:
        item = deepcopy(entity)
        entity_id = str(item["entity_id"])
        item["entity_id"] = entity_id
        item["domain"] = str(item.get("domain") or entity_id.split(".", 1)[0])
        attributes = dict(item.get("attributes", {}))
        if attributes.get("friendly_name") and not item.get("name"):
            item["name"] = attributes["friendly_name"]
        if attributes.get("unit_of_measurement") and "unit" not in attributes:
            attributes["unit"] = attributes["unit_of_measurement"]
        item["attributes"] = attributes
        if isinstance(item.get("state"), dict):
            return item

        raw_state = item.get("state", "unknown")
        domain = item["domain"]
        if domain in {"light", "switch"}:
            state: dict[str, Any] = {"power": _on_state(raw_state)}
            if "brightness" in attributes:
                state["brightness"] = round(float(attributes["brightness"]) * 100 / 255)
                item["supported_features"] = _with_feature(
                    item.get("supported_features"), "brightness"
                )
        elif domain == "cover":
            state = {"position": _scalar(attributes.get("current_position", raw_state))}
            if "current_position" in attributes:
                item["supported_features"] = _with_feature(
                    item.get("supported_features"), "position"
                )
        elif domain == "climate":
            state = {
                "temperature": _scalar(attributes.get("current_temperature", raw_state)),
                "target_temperature": _scalar(attributes.get("temperature")),
            }
            item["supported_features"] = _with_feature(
                item.get("supported_features"), "target_temperature"
            )
        elif domain == "sensor":
            measurement = str(attributes.get("measurement") or _sensor_measurement(attributes))
            attributes["measurement"] = measurement
            state = {measurement: _scalar(raw_state)}
        else:
            state = {"value": _scalar(raw_state)}
        item["state"] = state
        item["available"] = item.get("available", raw_state not in {"unknown", "unavailable"})
        return item

    def _translate_command(
        self, entity_id: str, command: ProviderCommand
    ) -> tuple[str, str, dict[str, Any]] | None:
        route = self._battery_route(entity_id, command.command)
        if route is not None:
            if route.service == "set_value":
                return self._translate_numeric_battery_route(route, command)
            if command.params.get("value") is not None:
                return None
        ev_route = self._ev_route(entity_id, command.command)
        if ev_route is not None:
            if ev_route.service == "set_value":
                return self._translate_numeric_battery_route(ev_route, command)
            if command.params.get("value") is not None:
                return None
        domain = entity_id.split(".", 1)[0]
        data: dict[str, Any] = {"entity_id": entity_id}
        if domain in {"light", "switch"} and command.command in {"turn_on", "turn_off", "toggle"}:
            return domain, command.command, data
        if domain == "light" and command.command == "set_brightness":
            value = command.params.get("value")
            if value is None:
                return None
            data["brightness_pct"] = value
            return domain, "turn_on", data
        if domain == "cover":
            services = {"open": "open_cover", "close": "close_cover", "stop": "stop_cover"}
            if command.command in services:
                return domain, services[command.command], data
            if command.command == "set_position" and command.params.get("value") is not None:
                data["position"] = command.params["value"]
                return domain, "set_cover_position", data
        if domain == "climate" and command.command == "set_temperature":
            value = command.params.get("value")
            if value is None:
                return None
            data["temperature"] = value
            return domain, "set_temperature", data
        return None

    def _battery_route(
        self, entity_id: str, command_name: str
    ) -> HomeAssistantBatteryCommandRoute | None:
        for binding in self.battery_dispatch_bindings.values():
            for route in (binding.charge, binding.discharge, binding.stop):
                if route.entity_id == entity_id and route.provider_command == command_name:
                    return route
        return None

    def _ev_route(
        self, entity_id: str, command_name: str
    ) -> HomeAssistantBatteryCommandRoute | None:
        for binding in self.ev_charging_bindings.values():
            for route in (binding.charge, binding.stop):
                if route.entity_id == entity_id and route.provider_command == command_name:
                    return route
        return None

    @staticmethod
    def _translate_numeric_battery_route(
        route: HomeAssistantBatteryCommandRoute, command: ProviderCommand
    ) -> tuple[str, str, dict[str, Any]] | None:
        raw_value = command.params.get("value")
        if route.value_transform == "zero":
            value: int | float = 0
        elif (
            isinstance(raw_value, (int, float))
            and not isinstance(raw_value, bool)
            and math.isfinite(float(raw_value))
        ):
            value = float(raw_value)
            if route.value_transform == "negate":
                value = -value
        else:
            return None
        return "number", "set_value", {"entity_id": route.entity_id, "value": value}

    def _result(
        self,
        command: ProviderCommand,
        status: ExecutionStatus,
        message: str,
        source_ref: SourceRef | None = None,
    ) -> ProviderExecutionResult:
        return ProviderExecutionResult(
            provider_id=self.manifest.provider_id,
            external_device_id=command.external_device_id,
            command=command.command,
            status=status,
            completed_at=self._clock.now(),
            message=message,
            source_ref=source_ref,
        )


def _state_changed_payload(message: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    event = message.get("event", message)
    if not isinstance(event, dict):
        return None, None
    data = event.get("data", event)
    if not isinstance(data, dict):
        return None, None
    entity_id = data.get("entity_id")
    new_state = data.get("new_state")
    return (
        str(entity_id) if entity_id else None,
        dict(new_state) if isinstance(new_state, dict) else None,
    )


def _finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _merge_capabilities(
    existing: list[Capability], additions: list[Capability]
) -> list[Capability]:
    merged = {capability.name: capability for capability in existing}
    for capability in additions:
        merged.setdefault(capability.name, capability)
    return list(merged.values())


def _with_feature(features: object, value: str) -> list[str]:
    existing = [str(item) for item in features] if isinstance(features, (list, tuple, set)) else []
    return _unique_strings([*existing, value])


def _sensor_measurement(attributes: Mapping[str, Any]) -> str:
    device_class = str(attributes.get("device_class", "value"))
    return {
        "temperature": "temperature",
        "humidity": "humidity",
        "power": "power",
        "energy": "energy",
        "battery": "battery",
    }.get(device_class, "value")


def _on_state(value: object) -> bool:
    return str(value).casefold() in {"on", "true", "1", "open"}


def _scalar(value: object) -> bool | int | float | str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    if text.casefold() in {"unknown", "unavailable", "none", "null", ""}:
        return None
    try:
        number = float(text)
    except ValueError:
        return text
    return int(number) if number.is_integer() else number


def _timestamp(*values: object, fallback: datetime) -> datetime:
    for value in values:
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC)
    return fallback


def _optional_text(value: object) -> str | None:
    return str(value) if value is not None and str(value) else None


def _first_text(*values: object) -> str | None:
    for value in values:
        result = _optional_text(value)
        if result is not None:
            return result
    return None


def _unique_strings(values: object) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        values = [values]
    return list(dict.fromkeys(str(value) for value in values if value is not None))


def _registry_values(values: object) -> list[str]:
    """Normalize HA registry identifiers and connections for configuration claims."""

    if not isinstance(values, (list, tuple, set)):
        values = [values]
    normalized: list[str] = []
    for value in values:
        if isinstance(value, (list, tuple)) and len(value) == 2:
            normalized.append(f"{value[0]}:{value[1]}")
        elif value is not None:
            normalized.append(str(value))
    return _unique_strings(normalized)
