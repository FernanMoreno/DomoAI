"""Safe, read-only importer for direct-topic MQTT Discovery documents."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from datetime import UTC, datetime

from pydantic import Field

from domoai.adapters.mqtt.config import (
    MqttAdapterMapping,
    MqttCapabilityMapping,
    MqttDeviceMapping,
)
from domoai.domain.connections import ConnectionDiagnostic, DiagnosticSeverity
from domoai.domain.models import CapabilityKind, StrictModel
from domoai.persistence.connection_documents import (
    ConnectionDocumentRepository,
    StoredConnectionDocument,
    canonical_json,
)
from domoai.runtime.mqtt_transport import MqttMessage, MqttTransport

_COMPONENTS = {"binary_sensor", "light", "number", "select", "sensor", "switch"}
_TOPIC_PATTERN = re.compile(r"^(?P<prefix>.+?)/(?P<component>[^/]+)/(?P<object_id>.+)/config$")
_FORBIDDEN_KEY = re.compile(r"(?:^|_)template(?:$|_)", re.IGNORECASE)
_SAFE_ID = re.compile(r"[^a-z0-9_.:-]+", re.IGNORECASE)


class MqttDiscoveryPreview(StrictModel):
    """The only value a caller needs before it explicitly persists a mapping."""

    adapter_id: str = Field(min_length=1, max_length=64)
    mapping: MqttAdapterMapping | None = None
    mapping_digest: str | None = Field(default=None, min_length=64, max_length=64)
    source_topics: tuple[str, ...] = ()
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    diagnostics: tuple[ConnectionDiagnostic, ...] = ()
    persistable: bool = False


class MqttDiscoveryImporter:
    """Translate a bounded Discovery read into an explicit MQTT mapping.

    This object never publishes. The only broker operation it performs is a
    read subscription to ``<prefix>/#`` during preview.
    """

    def __init__(
        self,
        transport: MqttTransport,
        *,
        discovery_prefix: str = "homeassistant",
        adapter_id: str = "mqtt-discovery",
        timeout: float = 5.0,
        max_messages: int = 256,
        max_payload_bytes: int = 64 * 1024,
        repository: ConnectionDocumentRepository | None = None,
    ) -> None:
        prefix = discovery_prefix.strip("/")
        if not prefix:
            raise ValueError("MQTT discovery prefix is required")
        if timeout <= 0 or max_messages <= 0 or max_payload_bytes <= 0:
            raise ValueError("MQTT discovery bounds must be positive")
        self.transport = transport
        self.discovery_prefix = prefix
        self.adapter_id = adapter_id
        self.timeout = timeout
        self.max_messages = max_messages
        self.max_payload_bytes = max_payload_bytes
        self.repository = repository

    async def preview(self) -> MqttDiscoveryPreview:
        if not await self.transport.health():
            await self.transport.connect()
        await self.transport.subscribe(f"{self.discovery_prefix}/#")
        deadline = asyncio.get_running_loop().time() + self.timeout
        messages: list[MqttMessage] = []
        diagnostics: list[ConnectionDiagnostic] = []
        while len(messages) < self.max_messages and asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            message = await self.transport.receive(min(remaining, 0.25))
            if message is None:
                break
            if self._topic_matches(message.topic):
                messages.append(message)
        devices: list[MqttDeviceMapping] = []
        topics: list[str] = []
        for message in messages:
            topics.append(message.topic)
            if len(message.payload) > self.max_payload_bytes:
                diagnostics.append(self._diagnostic_for_error("payload_too_large"))
                continue
            try:
                device = self._parse_message(message)
            except ValueError as error:
                diagnostics.extend(
                    self._diagnostic_for_error(reason)
                    for reason in str(error).split(",")
                    if reason
                )
                continue
            if device is not None:
                devices.append(device)
        mapping: MqttAdapterMapping | None = None
        digest: str | None = None
        if devices:
            try:
                mapping = MqttAdapterMapping(
                    schema_version="v1", adapter_id=self.adapter_id, devices=devices
                )
                digest = hashlib.sha256(
                    canonical_json(mapping.model_dump(mode="json")).encode("utf-8")
                ).hexdigest()
            except ValueError as error:
                diagnostics.append(
                    ConnectionDiagnostic(
                        code="mqtt_discovery_mapping_invalid",
                        severity=DiagnosticSeverity.ERROR,
                        retryable=False,
                        message="MQTT Discovery did not produce a valid mapping",
                        technical_details={"reason": str(error)[:180]},
                    )
                )
        diagnostics = self._unique_diagnostics(diagnostics)
        persistable = mapping is not None and not any(
            item.severity is DiagnosticSeverity.ERROR for item in diagnostics
        )
        return MqttDiscoveryPreview(
            adapter_id=self.adapter_id,
            mapping=mapping,
            mapping_digest=digest,
            source_topics=tuple(topics),
            diagnostics=tuple(diagnostics),
            persistable=persistable,
        )

    async def close(self) -> None:
        """Release the optional broker session owned by this importer."""

        await self.transport.disconnect()

    async def persist(
        self,
        preview: MqttDiscoveryPreview,
        *,
        confirmed: bool = False,
        confirm: bool | None = None,
    ) -> str | StoredConnectionDocument:
        if confirm is not None:
            confirmed = confirm
        if not confirmed:
            raise PermissionError("MQTT mapping persistence requires explicit confirmation")
        if not preview.persistable or preview.mapping is None or preview.mapping_digest is None:
            raise ValueError("MQTT Discovery preview is not persistable")
        digest = hashlib.sha256(
            canonical_json(preview.mapping.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()
        if digest != preview.mapping_digest:
            raise ValueError("MQTT Discovery preview digest does not match its mapping")
        provenance = {
            "discovery_prefix": self.discovery_prefix,
            "source_topics": list(preview.source_topics),
            "schema_version": "v1",
        }
        payload = preview.mapping.model_dump(mode="json")
        if self.repository is not None:
            return await self.repository.save(
                kind="mqtt_mapping", payload=payload, provenance=provenance
            )
        return f"mqtt_mapping:{preview.mapping_digest[:32]}"

    def _parse_message(self, message: MqttMessage) -> MqttDeviceMapping | None:
        match = _TOPIC_PATTERN.match(message.topic)
        if match is None or match.group("prefix").strip("/") != self.discovery_prefix:
            return None
        component = match.group("component")
        if component not in _COMPONENTS:
            raise ValueError("component_unsupported")
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("payload_invalid") from error
        if not isinstance(payload, dict) or not payload:
            raise ValueError("payload_invalid")
        forbidden_reasons: list[str] = []
        if any(_FORBIDDEN_KEY.search(str(key)) for key in payload):
            forbidden_reasons.append("template_forbidden")
        state_topic = self._topic(payload.get("state_topic"))
        command_topic = self._topic(payload.get("command_topic"))
        if state_topic is None:
            raise ValueError("state_topic_required")
        if command_topic is not None and any(token in command_topic for token in ("+", "#")):
            forbidden_reasons.append("wildcard_forbidden")
        if forbidden_reasons:
            raise ValueError(",".join(forbidden_reasons))
        if command_topic is not None and not isinstance(payload.get("command_topic"), str):
            raise ValueError("command_topic_invalid")
        object_id = match.group("object_id")
        source_id = self._source_id(payload.get("unique_id") or object_id)
        name = str(payload.get("name") or object_id)[:96]
        payload_on: str | None
        payload_off: str | None
        if component in {"binary_sensor", "switch", "light"}:
            kind = CapabilityKind.BOOLEAN
            commands = ["on", "off"] if command_topic is not None else []
            enum_values: list[str] = []
            payload_on = self._static_payload(payload.get("payload_on"), "ON")
            payload_off = self._static_payload(payload.get("payload_off"), "OFF")
        elif component == "number":
            kind = CapabilityKind.NUMBER
            commands = [name] if command_topic is not None else []
            enum_values = []
            payload_on = payload_off = None
        else:  # select
            options = payload.get("options")
            if not isinstance(options, list) or not options or not all(
                isinstance(option, str) and option for option in options
            ):
                raise ValueError("select_options_invalid")
            kind = CapabilityKind.ENUM
            enum_values = list(dict.fromkeys(options))
            commands = enum_values if command_topic is not None else []
            payload_on = payload_off = None
        capability = MqttCapabilityMapping(
            name=name,
            kind=kind,
            unit=self._optional_text(payload.get("unit_of_measurement")),
            minimum=self._number(payload.get("min")),
            maximum=self._number(payload.get("max")),
            enum_values=enum_values,
            commands=commands,
            state_topic=state_topic,
            command_topic=command_topic,
            feedback_topic=state_topic if command_topic is not None else None,
            require_readback=False,
            payload_on=payload_on,
            payload_off=payload_off,
        )
        return MqttDeviceMapping(
            source_id=source_id,
            type="light" if component == "light" else component,
            capabilities=[capability],
        )

    def _topic_matches(self, topic: str) -> bool:
        return topic.startswith(f"{self.discovery_prefix}/") and topic.endswith("/config")

    @staticmethod
    def _topic(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value or len(value) > 256:
            raise ValueError("topic_invalid")
        return value

    @staticmethod
    def _static_payload(value: object, default: str) -> str:
        if value is None:
            return default
        if not isinstance(value, str) or not value or len(value) > 64:
            raise ValueError("static_payload_invalid")
        return value

    @staticmethod
    def _optional_text(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > 32:
            raise ValueError("unit_invalid")
        return value

    @staticmethod
    def _number(value: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("number_invalid")
        normalized = float(value)
        if not math.isfinite(normalized):
            raise ValueError("number_invalid")
        return normalized

    @staticmethod
    def _source_id(value: object) -> str:
        raw = str(value).strip()
        normalized = _SAFE_ID.sub("-", raw).strip("-")
        if not normalized:
            raise ValueError("source_id_invalid")
        return f"mqtt:{normalized[:120]}"

    @staticmethod
    def _diagnostic_for_error(reason: str) -> ConnectionDiagnostic:
        code = {
            "component_unsupported": "mqtt_discovery_component_unsupported",
            "payload_invalid": "mqtt_discovery_payload_invalid",
            "template_forbidden": "mqtt_discovery_template_forbidden",
            "wildcard_forbidden": "mqtt_discovery_wildcard_forbidden",
            "payload_too_large": "mqtt_discovery_payload_too_large",
        }.get(reason, "mqtt_discovery_document_invalid")
        return ConnectionDiagnostic(
            code=code,
            severity=DiagnosticSeverity.ERROR,
            retryable=False,
            message="MQTT Discovery document was rejected",
            technical_details={"reason": reason},
        )

    @staticmethod
    def _unique_diagnostics(
        diagnostics: list[ConnectionDiagnostic],
    ) -> list[ConnectionDiagnostic]:
        seen: set[str] = set()
        result: list[ConnectionDiagnostic] = []
        for diagnostic in diagnostics:
            if diagnostic.code not in seen:
                seen.add(diagnostic.code)
                result.append(diagnostic)
        return result


__all__ = ["MqttDiscoveryImporter", "MqttDiscoveryPreview"]
