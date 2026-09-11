from __future__ import annotations

from domoai.application.connector_recipes import (
    ConnectorRecipeEngineRegistry,
    validate_connector_recipe,
    validate_recipe_endpoint,
)
from domoai.domain.connector_recipes import ConnectorRecipe


class _DiscoveryEngine:
    async def discover(self, recipe: ConnectorRecipe, setup: object) -> list[object]:
        return []


class _TransportEngine:
    def build_adapter(self, recipe: ConnectorRecipe, setup: object, *, adapter_id: str) -> object:
        return object()


def _recipe() -> ConnectorRecipe:
    return ConnectorRecipe.model_validate(
        {
            "connector_id": "acme_mqtt",
            "display_name": "Acme MQTT",
            "transport_family": "mqtt",
            "discovery_strategies": ["manual", "mqtt_discovery"],
            "capabilities": [
                {
                    "capability_id": "power",
                    "kind": "boolean",
                    "state_topic": "acme/{device_id}/state",
                }
            ],
        }
    )


def test_recipe_registry_reports_missing_transport_and_discovery_engines() -> None:
    diagnostics = ConnectorRecipeEngineRegistry().validate(_recipe())

    assert {diagnostic.code for diagnostic in diagnostics} == {
        "recipe_transport_unsupported",
        "recipe_discovery_unsupported",
    }


def test_recipe_registry_accepts_registered_engines_without_network_calls() -> None:
    registry = ConnectorRecipeEngineRegistry(
        discovery_engines={"manual": _DiscoveryEngine(), "mqtt_discovery": _DiscoveryEngine()},
        transport_engines={"mqtt": _TransportEngine()},
    )

    assert registry.validate(_recipe()) == ()


def test_public_recipe_validator_rejects_an_invalid_engine_registration() -> None:
    diagnostics = validate_connector_recipe(
        _recipe(), discovery_engines={"manual": object()}, transport_engines={"mqtt": object()}
    )

    assert {diagnostic.code for diagnostic in diagnostics} == {
        "recipe_transport_unsupported",
        "recipe_discovery_unsupported",
    }


def test_recipe_endpoint_allowlist_rejects_untrusted_host() -> None:
    diagnostics = validate_recipe_endpoint(
        {"url": "mqtt://broker.example:1883"},
        allowed_hosts=frozenset({"localhost"}),
    )

    assert diagnostics[0].code == "recipe_endpoint_not_allowed"


def test_recipe_endpoint_allowlist_accepts_local_host_and_does_not_expose_values() -> None:
    diagnostics = validate_recipe_endpoint(
        {"url": "mqtt://localhost:1883"},
        allowed_hosts=frozenset({"localhost"}),
    )

    assert diagnostics == ()
    assert "1883" not in "".join(item.message for item in diagnostics)
