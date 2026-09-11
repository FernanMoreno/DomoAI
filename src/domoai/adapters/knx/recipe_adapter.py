"""Declarative KNX recipe engine backed by the native adapter."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import cast

from domoai.adapters.knx.adapter import KnxAdapter
from domoai.adapters.knx.config import KnxMappingDocument
from domoai.adapters.knx.transport import KnxTransport
from domoai.domain.connector_recipes import ConnectorRecipe, KnxRecipeProfile
from domoai.runtime.connection_ports import ConnectionSetup


class KnxRecipeAdapter(KnxAdapter):
    """Compile one typed profile into the existing KNX semantic adapter."""

    def __init__(
        self,
        transport: KnxTransport,
        profile: KnxRecipeProfile,
        *,
        adapter_id: str = "knx-recipe",
    ) -> None:
        if profile.transport != "knx":
            raise ValueError("KNX recipe adapter requires a KNX profile")
        super().__init__(transport, _mapping(profile), adapter_id=adapter_id)
        self.recipe_profile = profile


def _mapping(profile: KnxRecipeProfile) -> KnxMappingDocument:
    entities: list[dict[str, object]] = []
    for entity in profile.entities:
        entity_data = entity.model_dump(mode="python", exclude_none=True)
        entity_data["capabilities"] = [
            binding.model_dump(mode="python", exclude={"readback_group_address"}, exclude_none=True)
            for binding in entity.capabilities
        ]
        entities.append(cast(dict[str, object], entity_data))
    return KnxMappingDocument.model_validate({"schema_version": "v1", "entities": entities})


class KnxRecipeTransport:
    """Engine that creates a recipe adapter using an injected KNX transport."""

    def __init__(
        self,
        transport_factory: Callable[
            [str, int, str], KnxTransport | Awaitable[KnxTransport]
        ],
    ) -> None:
        self.transport_factory = transport_factory

    async def build_adapter(
        self, recipe: ConnectorRecipe, setup: ConnectionSetup, *, adapter_id: str
    ) -> KnxRecipeAdapter:
        profile = recipe.protocol_profile
        if not isinstance(profile, KnxRecipeProfile):
            raise ValueError("recipe does not contain a KNX profile")
        host, port = _endpoint(setup)
        result = self.transport_factory(host, port, profile.secure_mode)
        transport = await result if inspect.isawaitable(result) else result
        return KnxRecipeAdapter(transport, profile, adapter_id=adapter_id)


def _endpoint(setup: ConnectionSetup) -> tuple[str, int]:
    host = setup.draft.endpoint.get("host")
    port = setup.draft.endpoint.get("port", 3671)
    if not isinstance(host, str) or not host.strip():
        raise ValueError("KNX recipe endpoint requires a host")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("KNX recipe endpoint port is invalid")
    return host.strip(), port


__all__ = ["KnxRecipeAdapter", "KnxRecipeTransport"]
