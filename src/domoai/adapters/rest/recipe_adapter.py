"""Normalized AdapterPort implementation for a bounded REST recipe."""

from __future__ import annotations

import json
import math
import re
from collections import OrderedDict
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from time import monotonic
from typing import Any, cast
from urllib.parse import quote, urljoin

import httpx

from domoai.adapters.rest.openapi_profile import OpenApiRecipeProfile
from domoai.domain.connections import ConnectionCandidate
from domoai.domain.connector_recipes import CapabilityBinding, ConnectorRecipe
from domoai.domain.models import (
    AdapterExecutionAck,
    AdapterHealth,
    AdapterSnapshot,
    CapabilityKind,
    Command,
    SourceEvent,
    SourceRef,
    StateSnapshot,
    StateStatus,
)
from domoai.runtime.connection_ports import ConnectionSetup
from domoai.runtime.execution_context import ExecutionContext

_JSON_PATH_TOKEN = re.compile(r"(?:\.([A-Za-z_][A-Za-z0-9_-]*)|\[(\d+)\])")
_RESPONSE_STREAM_CHUNK_BYTES = 64 * 1024
_DEFAULT_IDEMPOTENCY_CACHE_MAX_ENTRIES = 1024
_DEFAULT_IDEMPOTENCY_CACHE_TTL_SECONDS = 3600.0


class RestRecipeAdapter:
    """Execute only operations declared by both a recipe and OpenAPI profile."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        recipe: ConnectorRecipe,
        candidate: ConnectionCandidate,
        profile: OpenApiRecipeProfile,
        *,
        adapter_id: str,
        idempotency_cache_max_entries: int = _DEFAULT_IDEMPOTENCY_CACHE_MAX_ENTRIES,
        idempotency_cache_ttl_seconds: float = _DEFAULT_IDEMPOTENCY_CACHE_TTL_SECONDS,
    ) -> None:
        if recipe.transport_family.value != "rest":
            raise ValueError("REST recipe adapter requires the rest transport family")
        if candidate.connector_id != recipe.connector_id:
            raise ValueError("recipe and candidate connector IDs must match")
        self.client = client
        self.recipe = recipe
        self.candidate = candidate
        self.profile = profile
        self.adapter_id = adapter_id
        if (
            isinstance(idempotency_cache_max_entries, bool)
            or not isinstance(idempotency_cache_max_entries, int)
            or idempotency_cache_max_entries <= 0
        ):
            raise ValueError("idempotency cache max entries must be positive")
        if (
            isinstance(idempotency_cache_ttl_seconds, bool)
            or not isinstance(idempotency_cache_ttl_seconds, (int, float))
            or not math.isfinite(idempotency_cache_ttl_seconds)
            or idempotency_cache_ttl_seconds <= 0
        ):
            raise ValueError("idempotency cache TTL must be positive")
        self.idempotency_cache_max_entries = idempotency_cache_max_entries
        self.idempotency_cache_ttl_seconds = float(idempotency_cache_ttl_seconds)
        self._connected = False
        self._executed_idempotency_keys: OrderedDict[str, float] = OrderedDict()

        for binding in recipe.capabilities:
            if binding.read_path is None:
                raise ValueError("REST recipe capabilities require read_path")
            profile.operation("GET", binding.read_path)
            if binding.command is not None:
                command = binding.command
                if command.path is None or command.readback_path is None:
                    raise ValueError("REST recipe commands require path and readback_path")
                if command.method.upper() == "PUBLISH":
                    raise ValueError("REST recipe commands require an HTTP method")
                profile.operation(command.method, command.path)
                profile.operation("GET", command.readback_path)

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def discover(self) -> AdapterSnapshot:
        self._require_connected()
        capabilities = [
            {
                "name": binding.capability_id,
                "kind": binding.kind.value,
                "unit": binding.unit,
                "readable": True,
                "writable": binding.command is not None,
                "minimum": binding.minimum,
                "maximum": binding.maximum,
                "enum_values": list(binding.enum_values),
                "commands": [binding.command.name] if binding.command is not None else [],
            }
            for binding in self.recipe.capabilities
        ]
        return AdapterSnapshot(
            source_entities=[
                {
                    "entity_id": self.candidate.candidate_id,
                    "device_id": self.candidate.candidate_id,
                    "domain": self.recipe.device_type.value,
                    "semantic_type": self.recipe.device_type.value,
                    "name": self.candidate.display_name,
                    "protocol": "rest",
                    "capabilities": capabilities,
                    "available": True,
                }
            ]
        )

    async def read_state(self, source_refs: Sequence[SourceRef]) -> list[StateSnapshot]:
        self._require_connected()
        wanted = {source_ref.external_id for source_ref in source_refs}
        if self.candidate.candidate_id not in wanted:
            return []
        now = datetime.now(UTC)
        states: list[StateSnapshot] = []
        for binding in self.recipe.capabilities:
            if binding.read_path is None:
                continue
            payload = await self._request_json("GET", binding.read_path)
            value = _extract_json_path(payload, binding.read_json_path)
            _validate_value(binding, value)
            states.append(
                StateSnapshot(
                    device_id=self.candidate.candidate_id,
                    capability=binding.capability_id,
                    value=cast(Any, value),
                    unit=binding.unit,
                    observed_at=now,
                    received_at=datetime.now(UTC),
                    status=StateStatus.CURRENT,
                    source_ref=SourceRef(
                        adapter_id=self.adapter_id, external_id=self.candidate.candidate_id
                    ),
                )
            )
        return states

    async def execute(
        self, command: Command, execution_context: ExecutionContext | None = None
    ) -> AdapterExecutionAck:
        self._require_connected()
        if self._is_cached_idempotency_key(command.idempotency_key):
            return AdapterExecutionAck(
                accepted=True,
                source_ref=SourceRef(
                    adapter_id=self.adapter_id, external_id=self.candidate.candidate_id
                ),
                message="duplicate REST command suppressed",
            )
        if command.device_id != self.candidate.candidate_id:
            raise ValueError("command device is not declared by the REST recipe")
        for binding in self.recipe.capabilities:
            command_binding = binding.command
            if command_binding is None or command_binding.name != command.command:
                continue
            if command.unit is not None and command.unit != binding.unit:
                raise ValueError("REST command unit does not match mapping")
            _validate_value(binding, command.value)
            if command_binding.path is None or command_binding.readback_path is None:
                raise ValueError("REST command mapping is incomplete")
            rendered_payload = command_binding.payload_template.replace(
                "{value}", json.dumps(command.value, separators=(",", ":"))
            )
            try:
                payload: object = json.loads(rendered_payload)
            except json.JSONDecodeError:
                payload = rendered_payload
            await self._request_json(
                command_binding.method,
                command_binding.path,
                json_body=payload,
                headers={"Idempotency-Key": command.idempotency_key},
                execution_context=execution_context,
            )
            readback = await self._request_json("GET", command_binding.readback_path)
            observed = _extract_json_path(readback, command_binding.readback_json_path)
            _validate_value(binding, observed)
            if observed != command.value:
                return AdapterExecutionAck(
                    accepted=False,
                    source_ref=SourceRef(
                        adapter_id=self.adapter_id, external_id=self.candidate.candidate_id
                    ),
                    message="REST readback did not match command",
                )
            self._remember_idempotency_key(command.idempotency_key)
            return AdapterExecutionAck(
                accepted=True,
                source_ref=SourceRef(
                    adapter_id=self.adapter_id, external_id=self.candidate.candidate_id
                ),
                message="REST command executed and read back",
            )
        raise ValueError("command is not declared by the REST recipe")

    async def execute_source(
        self,
        command: Command,
        source_entity_id: str,
        execution_context: ExecutionContext | None = None,
    ) -> AdapterExecutionAck:
        source_command = command.model_copy(update={"device_id": source_entity_id})
        return await self.execute(source_command, execution_context)

    async def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        self._require_connected()
        if False:
            yield cast(SourceEvent, None)

    async def health(self) -> AdapterHealth:
        return AdapterHealth(adapter_id=self.adapter_id, connected=self._connected)

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: object | None = None,
        headers: Mapping[str, str] | None = None,
        execution_context: ExecutionContext | None = None,
    ) -> object:
        operation = self.profile.operation(method, path)
        rendered_path = path.replace(
            "{device_id}", quote(self.candidate.candidate_id, safe="")
        )
        url = urljoin(f"{self.profile.base_url}/", rendered_path.lstrip("/"))
        async with self.client.stream(
            operation.method,
            url,
            json=json_body,
            headers=headers,
            extensions={"domoai_execution_context": execution_context},
        ) as response:
            if response.is_redirect:
                raise ConnectionError("REST redirects are not allowed")
            if not 200 <= response.status_code < 300:
                raise ConnectionError("REST operation failed")
            content_length = response.headers.get("content-length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError:
                    declared_length = None
                if (
                    declared_length is not None
                    and declared_length > self.profile.max_response_bytes
                ):
                    raise ValueError("REST response is too large")
            body = bytearray()
            async for chunk in response.aiter_bytes(
                chunk_size=_RESPONSE_STREAM_CHUNK_BYTES
            ):
                if len(body) + len(chunk) > self.profile.max_response_bytes:
                    raise ValueError("REST response is too large")
                body.extend(chunk)
        try:
            payload = json.loads(bytes(body))
        except (TypeError, ValueError) as error:
            raise ValueError("REST response is not valid JSON") from error
        self.profile.validate_response(operation.method, path, payload)
        return payload

    def _is_cached_idempotency_key(self, key: str) -> bool:
        now = monotonic()
        self._prune_idempotency_cache(now)
        remembered_at = self._executed_idempotency_keys.get(key)
        if remembered_at is None:
            return False
        self._executed_idempotency_keys.move_to_end(key)
        return True

    def _remember_idempotency_key(self, key: str) -> None:
        now = monotonic()
        self._prune_idempotency_cache(now)
        self._executed_idempotency_keys[key] = now
        self._executed_idempotency_keys.move_to_end(key)
        while len(self._executed_idempotency_keys) > self.idempotency_cache_max_entries:
            self._executed_idempotency_keys.popitem(last=False)

    def _prune_idempotency_cache(self, now: float) -> None:
        expired = [
            key
            for key, remembered_at in self._executed_idempotency_keys.items()
            if now - remembered_at >= self.idempotency_cache_ttl_seconds
        ]
        for key in expired:
            del self._executed_idempotency_keys[key]

    def _require_connected(self) -> None:
        if not self._connected:
            raise ConnectionError("REST recipe adapter is not connected")


class RestRecipeTransport:
    """Engine bridge for callers that already own an HTTP client and profile."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        profile: OpenApiRecipeProfile,
        *,
        idempotency_cache_max_entries: int = _DEFAULT_IDEMPOTENCY_CACHE_MAX_ENTRIES,
        idempotency_cache_ttl_seconds: float = _DEFAULT_IDEMPOTENCY_CACHE_TTL_SECONDS,
    ) -> None:
        self.client = client
        self.profile = profile
        self.idempotency_cache_max_entries = idempotency_cache_max_entries
        self.idempotency_cache_ttl_seconds = idempotency_cache_ttl_seconds

    def build_adapter(
        self, recipe: ConnectorRecipe, setup: ConnectionSetup, *, adapter_id: str
    ) -> RestRecipeAdapter:
        return RestRecipeAdapter(
            self.client,
            recipe,
            setup.candidate,
            self.profile,
            adapter_id=adapter_id,
            idempotency_cache_max_entries=self.idempotency_cache_max_entries,
            idempotency_cache_ttl_seconds=self.idempotency_cache_ttl_seconds,
        )


def _extract_json_path(payload: object, path: str) -> object:
    if path == "$":
        return payload
    current = payload
    cursor = 1
    while cursor < len(path):
        match = _JSON_PATH_TOKEN.match(path, cursor)
        if match is None:
            raise ValueError("REST JSON path did not resolve")
        key, index = match.groups()
        if key is not None:
            if not isinstance(current, Mapping):
                raise ValueError("REST JSON path did not resolve")
            current = current.get(key)
        else:
            if isinstance(current, (str, bytes, bytearray)) or not isinstance(current, Sequence):
                raise ValueError("REST JSON path did not resolve")
            item_index = int(index)
            if item_index >= len(current):
                raise ValueError("REST JSON path did not resolve")
            current = current[item_index]
        if current is None:
            raise ValueError("REST JSON path did not resolve")
        cursor = match.end()
    return current


def _validate_value(binding: CapabilityBinding, value: object) -> None:
    kind = binding.kind
    if kind is CapabilityKind.BOOLEAN and not isinstance(value, bool):
        raise ValueError("REST value is not boolean")
    if kind is CapabilityKind.INTEGER and (
        not isinstance(value, int) or isinstance(value, bool)
    ):
        raise ValueError("REST value is not integer")
    if kind is CapabilityKind.NUMBER and (
        not isinstance(value, (int, float)) or isinstance(value, bool)
    ):
        raise ValueError("REST value is not numeric")
    if kind in {
        CapabilityKind.ENUM,
        CapabilityKind.TEXT,
        CapabilityKind.TIMESTAMP,
    } and not isinstance(value, str):
        raise ValueError("REST value is not text")
    if kind is CapabilityKind.ENUM and value not in binding.enum_values:
        raise ValueError("REST enum value is not declared")
    if binding.minimum is not None and isinstance(value, (int, float)) and value < binding.minimum:
        raise ValueError("REST value is below minimum")
    if binding.maximum is not None and isinstance(value, (int, float)) and value > binding.maximum:
        raise ValueError("REST value is above maximum")


__all__ = ["RestRecipeAdapter", "RestRecipeTransport"]
