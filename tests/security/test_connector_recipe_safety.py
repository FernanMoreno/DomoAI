from __future__ import annotations

import pytest

from domoai.application.connector_recipes import validate_recipe_endpoint
from domoai.domain.connector_recipes import ConnectorRecipe


def test_recipe_endpoint_rejects_embedded_credentials_before_network_access() -> None:
    with pytest.raises(ValueError, match="credential-bearing endpoint"):
        validate_recipe_endpoint(
            {"url": "mqtt://user:password@broker.example:1883"},
            allowed_hosts=frozenset({"broker.example"}),
        )


def test_recipe_validation_is_local_and_does_not_execute_recipe_content() -> None:
    recipe = ConnectorRecipe.model_validate(
        {
            "connector_id": "safe_recipe",
            "display_name": "Safe Recipe",
            "transport_family": "mqtt",
            "capabilities": [
                {
                    "capability_id": "power",
                    "kind": "boolean",
                    "state_topic": "safe/{device_id}/state",
                }
            ],
        }
    )

    assert recipe.connector_id == "safe_recipe"


def test_recipe_rejects_nested_json_authorization_without_echoing_secret() -> None:
    secret = "secret-bearer-value-123456"

    with pytest.raises(ValueError, match="secret material") as error:
        ConnectorRecipe.model_validate(
            {
                "connector_id": "safe_recipe",
                "display_name": "Safe Recipe",
                "transport_family": "mqtt",
                "discovery_options": {
                    "headers": '{"Authorization": "Bearer ' + secret + '"}'
                },
                "capabilities": [
                    {
                        "capability_id": "power",
                        "kind": "boolean",
                        "state_topic": "safe/{device_id}/state",
                    }
                ],
            }
        )

    assert secret not in str(error.value)


def test_recipe_endpoint_requires_an_explicit_network_policy() -> None:
    diagnostics = validate_recipe_endpoint(
        {"host": "169.254.169.254", "port": 80},
        allowed_hosts=frozenset(),
    )

    assert [diagnostic.code for diagnostic in diagnostics] == [
        "recipe_network_policy_missing"
    ]


def test_recipe_endpoint_allows_declared_cidr_and_port_only() -> None:
    allowed = validate_recipe_endpoint(
        {"host": "192.168.10.25", "port": 1883},
        allowed_hosts=frozenset(),
        allowed_networks=("192.168.10.0/24",),
        allowed_ports=frozenset({1883}),
    )
    rejected_port = validate_recipe_endpoint(
        {"host": "192.168.10.25", "port": 80},
        allowed_hosts=frozenset(),
        allowed_networks=("192.168.10.0/24",),
        allowed_ports=frozenset({1883}),
    )

    assert allowed == ()
    assert rejected_port[0].code == "recipe_endpoint_port_not_allowed"


def test_recipe_endpoint_allows_loopback_only_when_explicitly_declared() -> None:
    rejected = validate_recipe_endpoint(
        {"host": "127.0.0.1", "port": 1883},
        allowed_hosts=frozenset({"broker.local"}),
    )
    accepted = validate_recipe_endpoint(
        {"host": "127.0.0.1", "port": 1883},
        allowed_hosts=frozenset({"127.0.0.1"}),
    )

    assert rejected[0].code == "recipe_endpoint_not_allowed"
    assert accepted == ()
