"""Declarative, secret-free connector recipes."""

from __future__ import annotations

import json
import math
import re
import string
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from domoai.domain.models import CapabilityKind, DeviceType, ScalarValue, StrictModel

_CONNECTOR_ID = r"^[a-z0-9][a-z0-9_.-]*$"
_SENSITIVE_TEXT = re.compile(
    r"(?:token|password|passwd|secret|bearer|authorization|api[_-]?key|private[_-]?key)"
    r"\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_BEARER_VALUE = re.compile(
    r"\bbearer\s+(?P<value>[A-Za-z0-9][A-Za-z0-9._~+/\-=]{7,})",
    re.IGNORECASE,
)
_SENSITIVE_JSON_KEY = re.compile(
    r"^(?:authorization|token|access_token|refresh_token|password|passwd|secret|"
    r"client_secret|api_key|private_key|credential)$",
    re.IGNORECASE,
)
_SAFE_BEARER_WORDS = frozenset(
    {"protocol", "authentication", "authorization", "header", "token", "value"}
)
_SAFE_TEMPLATE_VALUE = re.compile(r"^(?:bearer\s+)?\{(?:value|device_id)\}$", re.IGNORECASE)
_CREDENTIAL_URL = re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s]+@", re.IGNORECASE)
_TEMPLATE_VARIABLES = frozenset({"device_id"})
_PAYLOAD_VARIABLES = frozenset({"value"})
_JSON_PATH = re.compile(r"^\$(?:\.[A-Za-z_][A-Za-z0-9_-]*|\[\d+\])*$")
_SAFE_PROFILE_ID = r"^[a-z0-9][a-z0-9_.-]*$"
_KNX_GROUP_ADDRESS = re.compile(r"^(\d+)/(\d+)(?:/(\d+))?$")

KNX_RECIPE_DPTS: dict[str, str] = {
    "power": "1.001",
    "brightness": "5.001",
    "temperature": "9.001",
    "humidity": "9.007",
    "occupancy": "1.018",
    "battery.soc": "13.013",
    "battery.power": "9.024",
    "battery.capacity": "13.013",
}

MATTER_CLUSTER_ATTRIBUTES: dict[str, tuple[int, int]] = {
    "power": (6, 0),
    "brightness": (8, 0),
    "temperature": (1026, 0),
    "humidity": (1029, 0),
    "occupancy": (1030, 0),
}
_MATTER_COMMANDS: dict[str, frozenset[str]] = {
    "power": frozenset({"On", "Off", "Toggle"}),
    "brightness": frozenset({"MoveToLevelWithOnOff"}),
    "temperature": frozenset(),
    "humidity": frozenset(),
    "occupancy": frozenset(),
}
_MODBUS_WRITABLE = frozenset(
    {"power", "brightness", "battery.power", "ev_charging", "thermal.hvac_power"}
)
_MODBUS_NUMERIC = frozenset(
    {
        "temperature",
        "humidity",
        "battery.soc",
        "battery.power",
        "battery.capacity",
        "ev.soc",
        "ev_charging",
        "ev.capacity",
        "water.flow_rate",
        "water.total_volume",
        "thermal.indoor_temperature",
        "thermal.hvac_power",
    }
)
_MODBUS_BOOLEAN = frozenset({"power", "occupancy", "ev.connected"})
_MODBUS_CAPABILITIES = _MODBUS_WRITABLE | _MODBUS_NUMERIC | _MODBUS_BOOLEAN
_KNX_CAPABILITIES = frozenset(KNX_RECIPE_DPTS)


class TransportFamily(StrEnum):
    MQTT = "mqtt"
    REST = "rest"
    WEBSOCKET = "websocket"
    MODBUS = "modbus"
    KNX = "knx"
    MATTER = "matter"
    SERIAL = "serial"
    CUSTOM = "custom"


class DiscoveryStrategy(StrEnum):
    MANUAL = "manual"
    MDNS = "mdns"
    SSDP = "ssdp"
    DHCP = "dhcp"
    MQTT_DISCOVERY = "mqtt_discovery"
    MATTER_COMMISSIONING = "matter_commissioning"


class AuthScheme(StrEnum):
    NONE = "none"
    USERNAME_PASSWORD = "username_password"
    TOKEN = "token"
    API_KEY = "api_key"
    TLS_CERTIFICATE = "tls_certificate"
    OAUTH2 = "oauth2"
    COMMISSIONING_CODE = "commissioning_code"


class ModbusRecipePoint(StrictModel):
    """One explicit Modbus address in a public recipe."""

    area: Literal["coil", "discrete_input", "input_register", "holding_register"]
    address: int = Field(strict=True, ge=0, le=65535)
    data_type: Literal["bool", "uint16", "int16", "float32"]
    byte_order: Literal["big", "little"] = "big"
    word_order: Literal["big", "little"] = "big"
    scale: float = 1.0
    offset: float = 0.0

    @property
    def register_count(self) -> int:
        return 2 if self.data_type == "float32" else 1

    @model_validator(mode="after")
    def validate_point(self) -> ModbusRecipePoint:
        if not math.isfinite(self.scale) or self.scale == 0:
            raise ValueError("Modbus scale must be finite and non-zero")
        if not math.isfinite(self.offset):
            raise ValueError("Modbus offset must be finite")
        if self.address + self.register_count > 65536:
            raise ValueError("Modbus point exceeds the 16-bit address space")
        if self.data_type == "bool":
            if self.area not in {"coil", "discrete_input"}:
                raise ValueError("boolean Modbus points require coil or discrete_input")
            if self.scale != 1 or self.offset != 0:
                raise ValueError("boolean Modbus points cannot use numeric conversion")
        elif self.area in {"coil", "discrete_input"}:
            raise ValueError("numeric Modbus points require a register area")
        return self


class ModbusRecipeBinding(StrictModel):
    name: str = Field(min_length=1, max_length=64)
    state: ModbusRecipePoint
    command: ModbusRecipePoint | None = None
    readback: ModbusRecipePoint | None = None

    @model_validator(mode="after")
    def validate_binding(self) -> ModbusRecipeBinding:
        if self.name not in _MODBUS_CAPABILITIES:
            raise ValueError("unknown Modbus capability")
        if self.command is not None and self.readback is None:
            raise ValueError("Modbus command requires an explicit readback point")
        if self.name in _MODBUS_BOOLEAN:
            if self.state.data_type != "bool" or self.state.area not in {
                "coil",
                "discrete_input",
            }:
                raise ValueError(f"{self.name} requires a boolean coil or discrete input")
            if self.command is not None and (
                self.command.data_type != "bool" or self.command.area != "coil"
            ):
                raise ValueError(f"{self.name} command requires a boolean coil")
        elif self.name in _MODBUS_NUMERIC:
            if self.state.data_type == "bool" or self.state.area not in {
                "input_register",
                "holding_register",
            }:
                raise ValueError(f"{self.name} requires a numeric register")
            if self.command is not None:
                if self.name not in _MODBUS_WRITABLE:
                    raise ValueError(f"{self.name} is read-only")
                if self.command.data_type == "bool" or self.command.area != "holding_register":
                    raise ValueError(f"{self.name} command requires a numeric holding register")
        return self


class ModbusRecipeEntity(StrictModel):
    entity_id: str = Field(min_length=1, max_length=96, pattern=_SAFE_PROFILE_ID)
    name: str = Field(min_length=1, max_length=128)
    semantic_type: Literal["light", "switch", "sensor", "energy"]
    area_id: str | None = Field(default=None, min_length=1, pattern=_SAFE_PROFILE_ID)
    unit_id: int = Field(strict=True, ge=1, le=247)
    capabilities: tuple[ModbusRecipeBinding, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_entity(self) -> ModbusRecipeEntity:
        names = [item.name for item in self.capabilities]
        if len(names) != len(set(names)):
            raise ValueError("duplicate Modbus capability")
        allowed = {
            "light": {"power", "brightness"},
            "switch": {"power"},
            "sensor": {"temperature", "humidity", "occupancy"},
            "energy": set(_MODBUS_CAPABILITIES) - {"power", "brightness", "occupancy"},
        }[self.semantic_type]
        unsupported = set(names) - allowed
        if unsupported:
            raise ValueError(f"unsupported capability: {sorted(unsupported)}")
        if self.semantic_type == "sensor" and any(item.command for item in self.capabilities):
            raise ValueError("sensor entities cannot be writable")
        return self


class ModbusRecipeProfile(StrictModel):
    transport: Literal["modbus"] = "modbus"
    entities: tuple[ModbusRecipeEntity, ...] = Field(min_length=1, max_length=256)
    unit_ids: tuple[int, ...] = Field(default=(), max_length=247)
    poll_interval_seconds: float = Field(default=5.0, gt=0, le=3600)

    @model_validator(mode="after")
    def validate_profile(self) -> ModbusRecipeProfile:
        if not math.isfinite(self.poll_interval_seconds):
            raise ValueError("poll_interval_seconds must be finite")
        if any(isinstance(item, bool) or not 1 <= item <= 247 for item in self.unit_ids):
            raise ValueError("Modbus unit_ids must be between 1 and 247")
        if len(set(self.unit_ids)) != len(self.unit_ids):
            raise ValueError("Modbus unit_ids must be unique")
        entity_ids = [entity.entity_id for entity in self.entities]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError("duplicate Modbus entity source ID")
        declared_units = set(self.unit_ids)
        if declared_units and any(entity.unit_id not in declared_units for entity in self.entities):
            raise ValueError("entity unit_id is not in the Modbus unit_ids allowlist")
        return self


class KnxRecipeBinding(StrictModel):
    name: str = Field(min_length=1, max_length=64)
    dpt: str = Field(min_length=5, max_length=16)
    state_group_address: str = Field(min_length=3, max_length=16)
    command_group_address: str | None = Field(default=None, min_length=3, max_length=16)
    readback_group_address: str | None = Field(default=None, min_length=3, max_length=16)

    @model_validator(mode="after")
    def validate_binding(self) -> KnxRecipeBinding:
        if self.name not in _KNX_CAPABILITIES:
            raise ValueError("unknown KNX capability")
        expected = KNX_RECIPE_DPTS[self.name]
        if self.dpt != expected:
            raise ValueError(f"{self.name} requires DPT {expected}")
        _validate_recipe_group_address(self.state_group_address)
        for value in (self.command_group_address, self.readback_group_address):
            if value is not None:
                _validate_recipe_group_address(value)
        if self.command_group_address is not None:
            if self.readback_group_address is None:
                raise ValueError("KNX command requires an explicit readback address")
            if self.name not in {"power", "brightness", "battery.power"}:
                raise ValueError(f"{self.name} is read-only")
        return self


class KnxRecipeEntity(StrictModel):
    entity_id: str = Field(min_length=1, max_length=96, pattern=_SAFE_PROFILE_ID)
    name: str = Field(min_length=1, max_length=128)
    semantic_type: Literal["light", "switch", "sensor", "energy"]
    area_id: str | None = Field(default=None, min_length=1, pattern=_SAFE_PROFILE_ID)
    capabilities: tuple[KnxRecipeBinding, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_entity(self) -> KnxRecipeEntity:
        names = [item.name for item in self.capabilities]
        if len(names) != len(set(names)):
            raise ValueError("duplicate KNX capability")
        allowed = {
            "light": {"power", "brightness"},
            "switch": {"power"},
            "sensor": {"temperature", "humidity", "occupancy"},
            "energy": {"battery.soc", "battery.power", "battery.capacity"},
        }[self.semantic_type]
        unsupported = set(names) - allowed
        if unsupported:
            raise ValueError(f"unsupported capability: {sorted(unsupported)}")
        if self.semantic_type == "sensor" and any(
            item.command_group_address for item in self.capabilities
        ):
            raise ValueError("sensor entities cannot be writable")
        return self


class KnxRecipeProfile(StrictModel):
    transport: Literal["knx"] = "knx"
    entities: tuple[KnxRecipeEntity, ...] = Field(min_length=1, max_length=256)
    secure_mode: Literal["none", "tunnel", "router"] = "none"
    group_dpts: dict[str, str] = Field(default_factory=dict, max_length=512)

    @model_validator(mode="after")
    def validate_profile(self) -> KnxRecipeProfile:
        entity_ids = [entity.entity_id for entity in self.entities]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError("duplicate KNX entity source ID")
        bindings = [binding for entity in self.entities for binding in entity.capabilities]
        expected_dpts = {
            binding.state_group_address: binding.dpt for binding in bindings
        }
        for address, dpt in self.group_dpts.items():
            _validate_recipe_group_address(address)
            if dpt not in set(KNX_RECIPE_DPTS.values()):
                raise ValueError(f"unsupported KNX DPT: {dpt}")
            matching_dpt = next(
                (item.dpt for item in bindings if item.state_group_address == address),
                dpt,
            )
            if dpt != matching_dpt:
                raise ValueError(f"KNX group address {address} has an inconsistent DPT")
        for address, dpt in expected_dpts.items():
            if address in self.group_dpts and self.group_dpts[address] != dpt:
                raise ValueError(f"KNX group address {address} has an inconsistent DPT")
        return self


class MatterRecipeBinding(StrictModel):
    name: str = Field(min_length=1, max_length=64)
    cluster_id: int = Field(strict=True, ge=0, le=65535)
    attribute_id: int = Field(strict=True, ge=0, le=65535)
    command_names: tuple[str, ...] = Field(default=(), max_length=16)
    readback: bool = False

    @model_validator(mode="after")
    def validate_binding(self) -> MatterRecipeBinding:
        expected = MATTER_CLUSTER_ATTRIBUTES.get(self.name)
        if expected is None:
            raise ValueError("unknown Matter capability")
        if (self.cluster_id, self.attribute_id) != expected:
            raise ValueError("Matter cluster/attribute is not the standard mapping")
        allowed = _MATTER_COMMANDS[self.name]
        if any(command not in allowed for command in self.command_names):
            raise ValueError("Matter command is not in the standard allowlist")
        if len(set(self.command_names)) != len(self.command_names):
            raise ValueError("Matter command names must be unique")
        if self.command_names and not self.readback:
            raise ValueError("Matter writable binding requires readback")
        return self


class MatterRecipeEndpoint(StrictModel):
    endpoint_id: int = Field(strict=True, ge=0, le=65535)
    device_type_ids: tuple[int, ...] = Field(default=(), max_length=32)
    capabilities: tuple[MatterRecipeBinding, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_endpoint(self) -> MatterRecipeEndpoint:
        if any(isinstance(item, bool) or not 0 <= item <= 65535 for item in self.device_type_ids):
            raise ValueError("Matter device_type_ids must be 16-bit integers")
        if len(set(self.device_type_ids)) != len(self.device_type_ids):
            raise ValueError("Matter device_type_ids must be unique")
        names = [item.name for item in self.capabilities]
        if len(names) != len(set(names)):
            raise ValueError("duplicate Matter capability")
        return self


class MatterRecipeProfile(StrictModel):
    transport: Literal["matter"] = "matter"
    endpoints: tuple[MatterRecipeEndpoint, ...] = Field(min_length=1, max_length=256)
    node_ids: tuple[int, ...] = Field(default=(), max_length=10000)
    commissioning_required: bool = True

    @model_validator(mode="after")
    def validate_profile(self) -> MatterRecipeProfile:
        if any(isinstance(item, bool) or item < 0 for item in self.node_ids):
            raise ValueError("Matter node_ids must be non-negative integers")
        if len(set(self.node_ids)) != len(self.node_ids):
            raise ValueError("Matter node_ids must be unique")
        endpoint_ids = [endpoint.endpoint_id for endpoint in self.endpoints]
        if len(endpoint_ids) != len(set(endpoint_ids)):
            raise ValueError("duplicate Matter endpoint profile")
        return self


ProtocolRecipeProfile = Annotated[
    ModbusRecipeProfile | KnxRecipeProfile | MatterRecipeProfile,
    Field(discriminator="transport"),
]


def _validate_recipe_group_address(value: str) -> None:
    match = _KNX_GROUP_ADDRESS.fullmatch(value)
    if match is None:
        raise ValueError(f"invalid KNX group address: {value}")
    parts = [int(part) for part in match.groups() if part is not None]
    if all(part == 0 for part in parts):
        raise ValueError("KNX broadcast group address is not allowed")
    if len(parts) == 3:
        if not 0 <= parts[0] <= 15 or not 0 <= parts[1] <= 15 or not 0 <= parts[2] <= 255:
            raise ValueError(f"KNX three-level group address is out of range: {value}")
    elif not 0 <= parts[0] <= 31 or not 0 <= parts[1] <= 2047:
        raise ValueError(f"KNX two-level group address is out of range: {value}")


def _check_public_text(value: str, field_name: str) -> str:
    if _contains_sensitive_text(value) or _CREDENTIAL_URL.search(value) is not None:
        raise ValueError(f"{field_name} contains secret material")
    return value


def _contains_sensitive_text(value: str, *, parse_json: bool = True) -> bool:
    if _SENSITIVE_TEXT.search(value) is not None or _has_bearer_material(value):
        return True
    if not parse_json:
        return False
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return False
    return _contains_sensitive_json(parsed)


def _contains_sensitive_json(value: object) -> bool:
    if isinstance(value, str):
        return _contains_sensitive_text(value, parse_json=False)
    if isinstance(value, dict):
        for key, nested in value.items():
            if isinstance(key, str) and _SENSITIVE_JSON_KEY.fullmatch(key):
                if not _is_safe_template_value(nested):
                    return True
            if _contains_sensitive_json(nested):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_sensitive_json(item) for item in value)
    return False


def _has_bearer_material(value: str) -> bool:
    return any(
        match.group("value").casefold() not in _SAFE_BEARER_WORDS
        for match in _BEARER_VALUE.finditer(value)
    )


def _is_safe_template_value(value: object) -> bool:
    return isinstance(value, str) and _SAFE_TEMPLATE_VALUE.fullmatch(value.strip()) is not None


def _check_template(value: str, field_name: str, allowed: frozenset[str]) -> str:
    _check_public_text(value, field_name)
    try:
        variables = {
            name
            for _, name, _, _ in string.Formatter().parse(value)
            if name is not None
        }
    except ValueError as error:
        raise ValueError(f"{field_name} template is invalid") from error
    unknown = variables - allowed
    if unknown:
        raise ValueError(f"{field_name} has unknown template variable")
    return value


def _check_json_path(value: str, field_name: str) -> str:
    _check_public_text(value, field_name)
    if _JSON_PATH.fullmatch(value) is None:
        raise ValueError(f"{field_name} JSON path is invalid")
    return value


class CommandBinding(StrictModel):
    """One explicitly allow-listed writable operation."""

    name: str = Field(min_length=1, max_length=96)
    method: str = Field(default="PUBLISH", min_length=1, max_length=16)
    command_topic: str | None = Field(default=None, min_length=1, max_length=256)
    path: str | None = Field(default=None, min_length=1, max_length=256)
    payload_template: str = Field(min_length=1, max_length=512)
    readback_topic: str | None = Field(default=None, min_length=1, max_length=256)
    readback_path: str | None = Field(default=None, min_length=1, max_length=256)
    readback_json_path: str = Field(default="$", min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_command(self) -> CommandBinding:
        allowed_methods = {"PUBLISH", "POST", "PUT", "PATCH"}
        if self.method.upper() not in allowed_methods:
            raise ValueError("method is not allowed")
        if self.command_topic is None and self.path is None:
            raise ValueError("command binding needs a topic or path")
        if self.readback_topic is None and self.readback_path is None:
            raise ValueError("command binding requires readback")
        _check_template(self.payload_template, "payload template", _PAYLOAD_VARIABLES)
        for field_name in ("command_topic", "path", "readback_topic", "readback_path"):
            value = getattr(self, field_name)
            if value is not None:
                _check_template(value, field_name, _TEMPLATE_VARIABLES)
        _check_json_path(self.readback_json_path, "readback_json_path")
        return self


class CapabilityBinding(StrictModel):
    """One normalized DomoAI capability and its optional write mapping."""

    capability_id: str = Field(min_length=1, max_length=96)
    kind: CapabilityKind
    unit: str | None = Field(default=None, min_length=1, max_length=32)
    minimum: float | int | None = None
    maximum: float | int | None = None
    enum_values: tuple[str, ...] = ()
    state_topic: str | None = Field(default=None, min_length=1, max_length=256)
    state_path: str | None = Field(default=None, min_length=1, max_length=256)
    read_path: str | None = Field(default=None, min_length=1, max_length=256)
    read_json_path: str = Field(default="$", min_length=1, max_length=128)
    command: CommandBinding | None = None

    @model_validator(mode="after")
    def validate_capability(self) -> CapabilityBinding:
        if self.state_topic is None and self.state_path is None and self.read_path is None:
            raise ValueError("capability binding needs a state topic, read path or path")
        if self.minimum is not None and (
            isinstance(self.minimum, bool) or not math.isfinite(float(self.minimum))
        ):
            raise ValueError("minimum must be a finite number")
        if self.maximum is not None and (
            isinstance(self.maximum, bool) or not math.isfinite(float(self.maximum))
        ):
            raise ValueError("maximum must be a finite number")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum must be less than or equal to maximum")
        if self.kind not in {CapabilityKind.INTEGER, CapabilityKind.NUMBER} and (
            self.minimum is not None or self.maximum is not None
        ):
            raise ValueError("numeric bounds require an integer or number capability")
        if self.kind is CapabilityKind.ENUM and not self.enum_values:
            raise ValueError("enum capabilities require enum_values")
        if len(set(self.enum_values)) != len(self.enum_values):
            raise ValueError("enum_values must contain unique values")
        for field_name in ("state_topic", "state_path"):
            value = getattr(self, field_name)
            if value is not None:
                _check_template(value, field_name, _TEMPLATE_VARIABLES)
        if self.read_path is not None:
            _check_template(self.read_path, "read_path", _TEMPLATE_VARIABLES)
        _check_json_path(self.read_json_path, "read_json_path")
        return self


class ConnectorRecipe(StrictModel):
    """Public recipe that can be used by a bounded generic connector engine."""

    schema_version: Literal["v1"] = "v1"
    connector_id: str = Field(min_length=1, max_length=64, pattern=_CONNECTOR_ID)
    display_name: str = Field(min_length=1, max_length=128)
    device_type: DeviceType = DeviceType.UNSUPPORTED
    transport_family: TransportFamily
    discovery_strategies: tuple[DiscoveryStrategy, ...] = Field(
        default=(DiscoveryStrategy.MANUAL,), min_length=1, max_length=8
    )
    auth_schemes: tuple[AuthScheme, ...] = Field(
        default=(AuthScheme.NONE,), min_length=1, max_length=8
    )
    discovery_options: dict[str, ScalarValue] = Field(default_factory=dict)
    capabilities: tuple[CapabilityBinding, ...] = Field(min_length=1, max_length=256)
    protocol_profile: ProtocolRecipeProfile | None = None

    @model_validator(mode="after")
    def validate_recipe(self) -> ConnectorRecipe:
        _check_public_text(self.display_name, "display_name")
        for key, value in self.discovery_options.items():
            if any(
                fragment in key.casefold()
                for fragment in ("token", "password", "secret", "credential", "key")
            ):
                raise ValueError("discovery options contain secret material")
            if isinstance(value, str):
                _check_public_text(value, f"discovery option {key}")
        if len(set(self.discovery_strategies)) != len(self.discovery_strategies):
            raise ValueError("discovery strategies must be unique")
        if len(set(self.auth_schemes)) != len(self.auth_schemes):
            raise ValueError("auth schemes must be unique")
        if AuthScheme.NONE in self.auth_schemes and len(self.auth_schemes) > 1:
            raise ValueError("none authentication cannot be combined with another scheme")
        capability_ids = [capability.capability_id for capability in self.capabilities]
        if len(capability_ids) != len(set(capability_ids)):
            raise ValueError("capability IDs must be unique")
        profile = self.protocol_profile
        protocol_families = {
            TransportFamily.MODBUS,
            TransportFamily.KNX,
            TransportFamily.MATTER,
        }
        if self.transport_family in protocol_families and profile is None:
            raise ValueError("protocol transport requires a typed protocol_profile")
        if profile is not None:
            if profile.transport != self.transport_family.value:
                raise ValueError("protocol_profile transport does not match transport_family")
            profile_ids = {
                binding.name
                for entity in getattr(profile, "entities", ())
                for binding in getattr(entity, "capabilities", ())
            }
            profile_ids.update(
                binding.name
                for endpoint in getattr(profile, "endpoints", ())
                for binding in getattr(endpoint, "capabilities", ())
            )
            if profile_ids != set(capability_ids):
                raise ValueError("recipe capabilities must match protocol_profile capabilities")
        elif self.transport_family in protocol_families:
            raise ValueError("protocol transport requires a typed protocol_profile")
        return self


__all__ = [
    "AuthScheme",
    "CapabilityBinding",
    "CommandBinding",
    "ConnectorRecipe",
    "DiscoveryStrategy",
    "KNX_RECIPE_DPTS",
    "MATTER_CLUSTER_ATTRIBUTES",
    "MatterRecipeBinding",
    "MatterRecipeEndpoint",
    "MatterRecipeProfile",
    "ProtocolRecipeProfile",
    "KnxRecipeBinding",
    "KnxRecipeEntity",
    "KnxRecipeProfile",
    "ModbusRecipeBinding",
    "ModbusRecipeEntity",
    "ModbusRecipePoint",
    "ModbusRecipeProfile",
    "TransportFamily",
]
