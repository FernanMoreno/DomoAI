"""Bounded discovery strategy for declarative MQTT connector recipes."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping

from domoai.domain.connections import ConnectionCandidate
from domoai.domain.connector_recipes import ConnectorRecipe
from domoai.runtime.connection_ports import ConnectionSetup
from domoai.runtime.mqtt_transport import MqttTransport

_ENDPOINT_KEYS = frozenset({"url", "base_url", "endpoint", "host", "port", "tls"})
_SENSITIVE_TEXT = re.compile(
    r"(?:token|password|passwd|secret|bearer|authorization|api[_-]?key)"
    r"\s*[:=]\s*\S+",
    re.IGNORECASE,
)


class MqttRecipeDiscovery:
    """Read candidates from one exact, retained MQTT discovery topic."""

    def __init__(
        self,
        transport: MqttTransport,
        *,
        timeout: float = 5.0,
        max_messages: int = 64,
        max_payload_bytes: int = 64 * 1024,
    ) -> None:
        if timeout <= 0 or max_messages <= 0 or max_payload_bytes <= 0:
            raise ValueError("MQTT recipe discovery bounds must be positive")
        self.transport = transport
        self.timeout = timeout
        self.max_messages = max_messages
        self.max_payload_bytes = max_payload_bytes

    async def discover(
        self, recipe: ConnectorRecipe, setup: ConnectionSetup
    ) -> list[ConnectionCandidate]:
        topic = self._topic(recipe)
        if not await self.transport.health():
            await self.transport.connect()
        await self.transport.subscribe(topic)
        deadline = asyncio.get_running_loop().time() + self.timeout
        candidates: dict[str, ConnectionCandidate] = {}
        messages = 0
        while messages < self.max_messages and asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            message = await self.transport.receive(min(remaining, 0.25))
            if message is None:
                break
            messages += 1
            if message.topic != topic or len(message.payload) > self.max_payload_bytes:
                continue
            candidate = self._candidate(recipe, setup, message.payload)
            if candidate is not None:
                candidates[candidate.candidate_id] = candidate
        return list(candidates.values())

    @staticmethod
    def _topic(recipe: ConnectorRecipe) -> str:
        topic_value = recipe.discovery_options.get("topic")
        if not isinstance(topic_value, str) or not topic_value.strip():
            raise ValueError("MQTT recipe discovery topic is required")
        topic = topic_value.strip().strip("/")
        if not topic or any(token in topic for token in ("+", "#")):
            raise ValueError("MQTT recipe discovery wildcard topic is forbidden")
        return topic

    @staticmethod
    def _candidate(
        recipe: ConnectorRecipe, setup: ConnectionSetup, payload: bytes
    ) -> ConnectionCandidate | None:
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(decoded, Mapping):
            return None
        candidate_id = decoded.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            return None
        display_name = decoded.get("display_name", candidate_id)
        if not isinstance(display_name, str) or not display_name.strip():
            return None
        if _SENSITIVE_TEXT.search(display_name) is not None:
            return None
        endpoint = {
            key: value
            for key, value in decoded.items()
            if key in _ENDPOINT_KEYS and isinstance(value, (str, int, float, bool))
        }
        if not endpoint:
            endpoint = dict(setup.draft.endpoint)
        try:
            return ConnectionCandidate(
                connector_id=recipe.connector_id,
                candidate_id=candidate_id.strip(),
                display_name=display_name.strip(),
                endpoint=endpoint,
                metadata={"source": "mqtt_recipe_discovery"},
            )
        except ValueError:
            return None


__all__ = ["MqttRecipeDiscovery"]
