from __future__ import annotations

import pytest
from pydantic import ValidationError

from domoai.domain.connector_recipes import (
    AuthScheme,
    CapabilityBinding,
    CommandBinding,
    ConnectorRecipe,
    DiscoveryStrategy,
    TransportFamily,
)
from domoai.domain.models import CapabilityKind


def _mqtt_recipe() -> dict[str, object]:
    return {
        "schema_version": "v1",
        "connector_id": "acme_mqtt",
        "display_name": "Acme MQTT",
        "transport_family": "mqtt",
        "discovery_strategies": ["manual", "mqtt_discovery"],
        "auth_schemes": ["username_password", "tls_certificate"],
        "capabilities": [
            {
                "capability_id": "power",
                "kind": "boolean",
                "state_topic": "acme/{device_id}/state",
                "command": {
                    "name": "power",
                    "command_topic": "acme/{device_id}/set",
                    "payload_template": "{value}",
                    "readback_topic": "acme/{device_id}/state",
                },
            }
        ],
    }


def test_connector_recipe_parses_public_mqtt_mapping() -> None:
    recipe = ConnectorRecipe.model_validate(_mqtt_recipe())

    assert recipe.transport_family is TransportFamily.MQTT
    assert recipe.discovery_strategies == (
        DiscoveryStrategy.MANUAL,
        DiscoveryStrategy.MQTT_DISCOVERY,
    )
    assert recipe.auth_schemes == (AuthScheme.USERNAME_PASSWORD, AuthScheme.TLS_CERTIFICATE)
    assert recipe.capabilities[0].command is not None


def test_recipe_rejects_duplicate_capabilities_and_unsupported_method() -> None:
    payload = _mqtt_recipe()
    payload["capabilities"] = [
        {
            "capability_id": "power",
            "kind": "boolean",
            "state_topic": "acme/{device_id}/state",
        },
        {
            "capability_id": "power",
            "kind": "boolean",
            "state_topic": "acme/{device_id}/other",
        },
    ]

    with pytest.raises(ValidationError, match="capability IDs must be unique"):
        ConnectorRecipe.model_validate(payload)

    payload = _mqtt_recipe()
    payload["capabilities"] = [
        {
            "capability_id": "power",
            "kind": "boolean",
            "state_topic": "acme/{device_id}/state",
            "command": {
                "name": "power",
                "command_topic": "acme/{device_id}/set",
                "payload_template": "{value}",
                "readback_topic": "acme/{device_id}/state",
                "method": "DELETE",
            },
        }
    ]

    with pytest.raises(ValidationError, match="method is not allowed"):
        ConnectorRecipe.model_validate(payload)


def test_recipe_rejects_secrets_and_write_without_readback() -> None:
    payload = _mqtt_recipe()
    payload["display_name"] = "Acme token=do-not-store"
    with pytest.raises(ValidationError, match="secret material"):
        ConnectorRecipe.model_validate(payload)

    payload = _mqtt_recipe()
    payload["capabilities"] = [
        {
            "capability_id": "power",
            "kind": "boolean",
            "state_topic": "acme/{device_id}/state",
            "command": {
                "name": "power",
                "command_topic": "acme/{device_id}/set",
                "payload_template": "{value}",
            },
        }
    ]
    with pytest.raises(ValidationError, match="readback"):
        ConnectorRecipe.model_validate(payload)


def test_recipe_rejects_ambiguous_templates_and_invalid_bounds() -> None:
    with pytest.raises(ValidationError, match="unknown template variable"):
        CapabilityBinding(
            capability_id="temperature",
            kind=CapabilityKind.NUMBER,
            state_topic="acme/{unknown}/state",
        )

    with pytest.raises(ValidationError, match="minimum must be less"):
        CapabilityBinding(
            capability_id="temperature",
            kind=CapabilityKind.NUMBER,
            state_topic="acme/{device_id}/state",
            minimum=30,
            maximum=10,
        )

    with pytest.raises(ValidationError, match="payload template"):
        CommandBinding(
            name="power",
            command_topic="acme/{device_id}/set",
            payload_template="{device_id}",
            readback_topic="acme/{device_id}/state",
        )


def test_recipe_rejects_secret_discovery_options() -> None:
    payload = _mqtt_recipe()
    payload["discovery_options"] = {"broker_password": "do-not-store"}

    with pytest.raises(ValidationError, match="discovery options contain secret material"):
        ConnectorRecipe.model_validate(payload)


@pytest.mark.parametrize(
    "field",
    ("display_name", "state_topic", "payload_template", "discovery_options"),
)
def test_recipe_rejects_bearer_tokens_in_all_public_text_fields(field: str) -> None:
    secret = "eyJhbGciOiJIUzI1NiJ9.payload.signature"
    payload = _mqtt_recipe()
    if field == "display_name":
        payload[field] = f"Acme Bearer {secret}"
    elif field == "state_topic":
        payload["capabilities"][0][field] = f"Bearer {secret}"
    elif field == "payload_template":
        payload["capabilities"][0]["command"][field] = (
            f'Bearer {secret} {{value}}'
        )
    else:
        payload[field] = {"headers": '{"Authorization": "Bearer ' + secret + '"}'}

    with pytest.raises(ValidationError, match="secret material") as error:
        ConnectorRecipe.model_validate(payload)

    assert secret not in str(error.value)


def test_recipe_accepts_descriptive_bearer_text_and_approved_templates() -> None:
    payload = _mqtt_recipe()
    payload["display_name"] = "Bearer protocol"
    payload["discovery_options"] = {"description": "Bearer protocol"}

    recipe = ConnectorRecipe.model_validate(payload)

    assert recipe.display_name == "Bearer protocol"


def test_rest_recipe_can_declare_read_and_readback_json_paths() -> None:
    recipe = ConnectorRecipe.model_validate(
        {
            "connector_id": "rest_fixture",
            "display_name": "REST Fixture",
            "transport_family": "rest",
            "capabilities": [
                {
                    "capability_id": "power",
                    "kind": "boolean",
                    "read_path": "/devices/{device_id}",
                    "read_json_path": "$.power",
                    "command": {
                        "name": "set_power",
                        "method": "PUT",
                        "path": "/devices/{device_id}/power",
                        "payload_template": "{value}",
                        "readback_path": "/devices/{device_id}",
                        "readback_json_path": "$.power",
                    },
                }
            ],
        }
    )

    assert recipe.capabilities[0].read_json_path == "$.power"
    assert recipe.capabilities[0].command is not None
    assert recipe.capabilities[0].command.readback_json_path == "$.power"


def test_recipe_rejects_unsafe_json_paths() -> None:
    with pytest.raises(ValidationError, match="JSON path"):
        ConnectorRecipe.model_validate(
            {
                "connector_id": "rest_fixture",
                "display_name": "REST Fixture",
                "transport_family": "rest",
                "capabilities": [
                    {
                        "capability_id": "power",
                        "kind": "boolean",
                        "read_path": "/devices/{device_id}",
                        "read_json_path": "../../secret",
                    }
                ],
            }
        )
