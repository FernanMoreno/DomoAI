"""Declarative Matter recipe engine backed by Matter Server."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from domoai.adapters.matter.adapter import MatterServerAdapter
from domoai.adapters.matter.mapper import DESCRIPTOR_CLUSTER, MatterMapper
from domoai.adapters.matter.transport import MatterTransport
from domoai.domain.connector_recipes import ConnectorRecipe, MatterRecipeProfile
from domoai.runtime.connection_ports import ConnectionSetup


class MatterRecipeAdapter(MatterServerAdapter):
    """Expose only the node, endpoint and standard interactions in a profile."""

    def __init__(
        self,
        transport: MatterTransport,
        profile: MatterRecipeProfile,
        *,
        adapter_id: str = "matter-recipe",
    ) -> None:
        if profile.transport != "matter":
            raise ValueError("Matter recipe adapter requires a Matter profile")
        super().__init__(transport, adapter_id=adapter_id)
        self.recipe_profile = profile
        self._profile_endpoints = {item.endpoint_id: item for item in profile.endpoints}

    def _ingest_node(self, node: dict[str, Any]) -> None:
        node_id = node.get("node_id")
        if isinstance(node_id, bool) or not isinstance(node_id, int) or node_id < 0:
            raise ValueError("Matter node_id must be a non-negative integer")
        if self.recipe_profile.node_ids and node_id not in self.recipe_profile.node_ids:
            self._drop_node(node_id)
            return
        super()._ingest_node(node)
        self._filter_node(node_id, node)

    def _filter_node(self, node_id: int, node: dict[str, Any]) -> None:
        attributes = node.get("attributes")
        if not isinstance(attributes, dict):
            return
        allowed_sources: set[str] = set()
        for endpoint_id, profile_endpoint in self._profile_endpoints.items():
            descriptor_path = MatterMapper.path(endpoint_id, DESCRIPTOR_CLUSTER, 0)
            device_types = _device_types(attributes.get(descriptor_path))
            if profile_endpoint.device_type_ids and not device_types.intersection(
                profile_endpoint.device_type_ids
            ):
                continue
            source = MatterMapper.source_entity_id(node_id, endpoint_id)
            entity = self._source_entities.get(source)
            if entity is None:
                continue
            allowed_names = {binding.name for binding in profile_endpoint.capabilities}
            entity["capabilities"] = [
                capability
                for capability in entity.get("capabilities", [])
                if capability.get("name") in allowed_names
            ]
            if entity["capabilities"]:
                allowed_sources.add(source)
        self._source_entities = {
            source: entity
            for source, entity in self._source_entities.items()
            if source in allowed_sources
        }
        allowed_capabilities = {
            (source, binding.name)
            for endpoint_id, endpoint in self._profile_endpoints.items()
            for binding in endpoint.capabilities
            for source in allowed_sources
            if source.endswith(f"/endpoint:{endpoint_id}")
        }
        self._states = {
            key: state for key, state in self._states.items() if key in allowed_capabilities
        }
        self._unsupported = [
            item for item in self._unsupported if str(item.get("entity_id")) in allowed_sources
        ]
        self._remember_sources()

    def _drop_node(self, node_id: int) -> None:
        prefix = f"node:{node_id}/endpoint:"
        self._nodes.pop(node_id, None)
        self._source_entities = {
            source: entity
            for source, entity in self._source_entities.items()
            if not source.startswith(prefix)
        }
        self._states = {
            key: state for key, state in self._states.items() if not key[0].startswith(prefix)
        }
        self._unsupported = [
            item
            for item in self._unsupported
            if not str(item.get("entity_id", "")).startswith(prefix)
        ]
        self._remember_sources()


def _device_types(value: Any) -> set[int]:
    if not isinstance(value, list):
        return set()
    result: set[int] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        raw = item.get("deviceType", item.get(0))
        if isinstance(raw, int) and not isinstance(raw, bool):
            result.add(raw)
    return result


class MatterRecipeTransport:
    """Engine that creates a recipe adapter from a commissioned Matter Server."""

    def __init__(
        self,
        transport_factory: Callable[[str], MatterTransport | Awaitable[MatterTransport]],
    ) -> None:
        self.transport_factory = transport_factory

    async def build_adapter(
        self, recipe: ConnectorRecipe, setup: ConnectionSetup, *, adapter_id: str
    ) -> MatterRecipeAdapter:
        profile = recipe.protocol_profile
        if not isinstance(profile, MatterRecipeProfile):
            raise ValueError("recipe does not contain a Matter profile")
        endpoint = setup.draft.endpoint.get("url")
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("Matter recipe endpoint requires a server URL")
        result = self.transport_factory(endpoint.strip())
        transport = await result if inspect.isawaitable(result) else result
        return MatterRecipeAdapter(transport, profile, adapter_id=adapter_id)


__all__ = ["MatterRecipeAdapter", "MatterRecipeTransport"]
