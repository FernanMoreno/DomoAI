"""Declarative Modbus recipe engine backed by the native adapter."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import cast

from domoai.adapters.modbus.adapter import ModbusAdapter
from domoai.adapters.modbus.config import ModbusMappingDocument
from domoai.adapters.modbus.transport import ModbusTransport
from domoai.domain.connector_recipes import ConnectorRecipe, ModbusRecipeProfile
from domoai.runtime.connection_ports import ConnectionSetup


class ModbusRecipeAdapter(ModbusAdapter):
    """Compile one typed profile into the existing Modbus semantic adapter."""

    def __init__(
        self,
        transport: ModbusTransport,
        profile: ModbusRecipeProfile,
        *,
        adapter_id: str = "modbus-recipe",
    ) -> None:
        if profile.transport != "modbus":
            raise ValueError("Modbus recipe adapter requires a Modbus profile")
        super().__init__(
            transport,
            _mapping(profile),
            adapter_id=adapter_id,
            poll_interval=profile.poll_interval_seconds,
        )
        self.recipe_profile = profile


def _mapping(profile: ModbusRecipeProfile) -> ModbusMappingDocument:
    entities: list[dict[str, object]] = []
    for entity in profile.entities:
        for binding in entity.capabilities:
            if binding.command is not None and binding.readback != binding.state:
                raise ValueError(
                    f"Modbus readback for {entity.entity_id}/{binding.name} must equal state"
                )
        entity_data = entity.model_dump(mode="python", exclude_none=True)
        entity_data["capabilities"] = [
            binding.model_dump(mode="python", exclude={"readback"}, exclude_none=True)
            for binding in entity.capabilities
        ]
        entities.append(cast(dict[str, object], entity_data))
    return ModbusMappingDocument.model_validate({"schema_version": "v1", "entities": entities})


class ModbusRecipeTransport:
    """Engine that creates a recipe adapter using an injected transport."""

    def __init__(
        self,
        transport_factory: Callable[
            [str, int], ModbusTransport | Awaitable[ModbusTransport]
        ],
    ) -> None:
        self.transport_factory = transport_factory

    async def build_adapter(
        self, recipe: ConnectorRecipe, setup: ConnectionSetup, *, adapter_id: str
    ) -> ModbusRecipeAdapter:
        profile = recipe.protocol_profile
        if not isinstance(profile, ModbusRecipeProfile):
            raise ValueError("recipe does not contain a Modbus profile")
        host, port = _endpoint(setup)
        result = self.transport_factory(host, port)
        transport = await result if inspect.isawaitable(result) else result
        return ModbusRecipeAdapter(transport, profile, adapter_id=adapter_id)


def _endpoint(setup: ConnectionSetup) -> tuple[str, int]:
    host = setup.draft.endpoint.get("host")
    port = setup.draft.endpoint.get("port", 502)
    if not isinstance(host, str) or not host.strip():
        raise ValueError("Modbus recipe endpoint requires a host")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Modbus recipe endpoint port is invalid")
    return host.strip(), port


__all__ = ["ModbusRecipeAdapter", "ModbusRecipeTransport"]
