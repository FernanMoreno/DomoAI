"""Home Assistant implementation of the Provider SDK v1 boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from domoai.adapters.home_assistant.client import HomeAssistantClient
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
    ) -> None:
        self.client = client
        self.mapper = HomeAssistantMapper()
        self.metric_mappings = {
            str(entity_id): {str(capability): str(metric) for capability, metric in mapping.items()}
            for entity_id, mapping in (metric_mappings or {}).items()
        }
        self._entities: dict[str, dict[str, Any]] = {}
        self._entity_device: dict[str, str] = {}
        self._entity_commands: dict[str, set[str]] = {}
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

    async def execute(self, command: ProviderCommand) -> ProviderExecutionResult:
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
            return self._result(command, ExecutionStatus.REJECTED, "Invalid Home Assistant command")
        domain, service, data = translated
        try:
            await self.client.call_service(domain, service, data)
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
            self._entities[entity_id] = entity
            snapshot = self.mapper.to_snapshot([self._normalize_entity(entity)])
            for measurement in self._measurements_for_snapshot(snapshot.source_states, None):
                yield measurement

    async def _load_snapshot(self) -> AdapterSnapshot:
        entities = await self.client.fetch_states()
        entities = await self._merge_entity_registry(entities)
        normalized = [self._normalize_entity(entity) for entity in entities]
        snapshot = self.mapper.to_snapshot(normalized)
        self._remember_routes(normalized, snapshot.source_entities)
        return snapshot

    async def _merge_entity_registry(self, entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not entities or all(entity.get("device_id") for entity in entities):
            return entities
        fetch_registry = getattr(self.client, "fetch_entity_registry", None)
        if not callable(fetch_registry):
            return entities
        try:
            registry_entries = await fetch_registry()
        except Exception:
            return entities
        registry = {
            str(entry["entity_id"]): entry
            for entry in registry_entries
            if isinstance(entry, dict) and entry.get("entity_id")
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

    def _measurements_for_snapshot(
        self, source_states: list[dict[str, Any]], allowed_devices: set[str] | None
    ) -> list[Measurement]:
        received_at = datetime.now(UTC)
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
            metric = self._metric_for(entity_id, str(state["capability"]))
            if metric is None:
                continue
            raw = self._entities.get(entity_id, {})
            observed_at = _timestamp(
                raw.get("last_updated"), raw.get("last_changed"), fallback=received_at
            )
            measurement_received_at = max(received_at, observed_at)
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

    @staticmethod
    def _translate_command(
        entity_id: str, command: ProviderCommand
    ) -> tuple[str, str, dict[str, Any]] | None:
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
            completed_at=datetime.now(UTC),
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
