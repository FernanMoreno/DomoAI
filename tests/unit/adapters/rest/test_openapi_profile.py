from __future__ import annotations

import pytest

from domoai.adapters.rest.openapi_profile import OpenApiRecipeProfile
from domoai.adapters.rest.recipe_adapter import _extract_json_path


def _document() -> dict[str, object]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Fixture", "version": "1.0"},
        "paths": {
            "/devices/{device_id}": {
                "get": {
                    "operationId": "get_device",
                    "responses": {
                        "200": {
                            "description": "Current state",
                            "content": {
                                "application/json": {"schema": {"type": "object"}}
                            },
                        }
                    },
                }
            },
            "/devices/{device_id}/power": {
                "put": {
                    "operationId": "set_power",
                    "responses": {
                        "200": {
                            "description": "Accepted",
                            "content": {
                                "application/json": {"schema": {"type": "object"}}
                            },
                        }
                    },
                }
            },
        },
    }


def test_openapi_profile_allowlists_declared_json_operations() -> None:
    profile = OpenApiRecipeProfile.from_document(
        _document(),
        endpoint="https://api.example.test",
        allowed_hosts=frozenset({"api.example.test"}),
    )

    operation = profile.operation("GET", "/devices/{device_id}")
    assert operation.operation_id == "get_device"
    assert profile.base_url == "https://api.example.test"


def test_openapi_profile_rejects_missing_schema_and_unsafe_method() -> None:
    document = _document()
    paths = document["paths"]
    assert isinstance(paths, dict)
    paths["/devices/{device_id}"]["delete"] = {
        "responses": {"204": {"description": "Deleted"}}
    }

    with pytest.raises(ValueError, match="HTTP method"):
        OpenApiRecipeProfile.from_document(document, endpoint="https://api.example.test")

    document = _document()
    paths = document["paths"]
    assert isinstance(paths, dict)
    paths["/devices/{device_id}"]["get"]["responses"] = {
        "200": {"description": "Missing schema"}
    }
    with pytest.raises(ValueError, match="response schema"):
        OpenApiRecipeProfile.from_document(document, endpoint="https://api.example.test")


def test_openapi_profile_rejects_path_traversal_and_untrusted_server() -> None:
    document = _document()
    paths = document["paths"]
    assert isinstance(paths, dict)
    paths["/../admin"] = paths.pop("/devices/{device_id}")

    with pytest.raises(ValueError, match="path"):
        OpenApiRecipeProfile.from_document(document, endpoint="https://api.example.test")

    with pytest.raises(ValueError, match="allowlist"):
        OpenApiRecipeProfile.from_document(
            _document(),
            endpoint="https://other.example.test",
            allowed_hosts=frozenset({"api.example.test"}),
        )


def test_rest_json_path_resolves_bounded_array_indexes() -> None:
    payload = {"items": [{"power": True}]}

    assert _extract_json_path(payload, "$.items[0].power") is True
