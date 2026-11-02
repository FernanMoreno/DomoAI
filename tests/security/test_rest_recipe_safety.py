from __future__ import annotations

import pytest

from domoai.adapters.rest.openapi_profile import OpenApiRecipeProfile


def test_rest_profile_rejects_credential_urls_and_non_https_remote_endpoints() -> None:
    with pytest.raises(ValueError, match="credentials"):
        OpenApiRecipeProfile.from_document({}, endpoint="https://user:pass@api.example.test")

    with pytest.raises(ValueError, match="HTTPS"):
        OpenApiRecipeProfile.from_document({}, endpoint="http://api.example.test")


def test_rest_profile_rejects_oversized_documents_and_non_json_response() -> None:
    with pytest.raises(ValueError, match="too large"):
        OpenApiRecipeProfile.from_document(
            {"openapi": "3.1.0", "paths": {}},
            endpoint="https://api.example.test",
            max_document_bytes=10,
        )


def test_rest_profile_requires_a_typed_json_response_schema() -> None:
    with pytest.raises(ValueError, match="response schema"):
        OpenApiRecipeProfile.from_document(
            {
                "openapi": "3.1.0",
                "paths": {
                    "/devices": {
                        "get": {
                            "responses": {
                                "200": {
                                    "description": "untyped",
                                    "content": {"application/json": {"schema": {}}},
                                }
                            }
                        }
                    }
                },
            },
            endpoint="https://api.example.test",
        )


def test_rest_profile_rejects_remote_schema_refs_before_any_network_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[object] = []

    def unexpected_network(*args: object, **kwargs: object) -> None:
        requests.append((args, kwargs))
        raise AssertionError("schema validation attempted network access")

    monkeypatch.setattr("urllib.request.urlopen", unexpected_network)
    with pytest.raises(ValueError, match="remote.*ref|reference.*local"):
        OpenApiRecipeProfile.from_document(
            {
                "openapi": "3.1.0",
                "paths": {
                    "/devices": {
                        "get": {
                            "responses": {
                                "200": {
                                    "content": {
                                        "application/json": {
                                            "schema": {
                                                "type": "object",
                                                "$ref": "https://metadata.example.test/schema",
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                },
            },
            endpoint="https://api.example.test",
        )
    assert requests == []


def test_rest_profile_resolves_local_schema_refs_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[object] = []

    def unexpected_network(*args: object, **kwargs: object) -> None:
        requests.append((args, kwargs))
        raise AssertionError("local schema validation attempted network access")

    monkeypatch.setattr("urllib.request.urlopen", unexpected_network)
    profile = OpenApiRecipeProfile.from_document(
        {
            "openapi": "3.1.0",
            "components": {
                "schemas": {
                    "DeviceState": {
                        "type": "object",
                        "required": ["power"],
                        "properties": {"power": {"type": "boolean"}},
                    }
                }
            },
            "paths": {
                "/devices": {
                    "get": {
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/DeviceState"}
                                    }
                                }
                            }
                        }
                    }
                }
            },
        },
        endpoint="https://api.example.test",
    )

    profile.validate_response("GET", "/devices", {"power": True})
    assert requests == []


def test_rest_profile_rejects_unresolved_local_schema_refs() -> None:
    with pytest.raises(ValueError, match="unresolved"):
        OpenApiRecipeProfile.from_document(
            {
                "openapi": "3.1.0",
                "paths": {
                    "/devices": {
                        "get": {
                            "responses": {
                                "200": {
                                    "content": {
                                        "application/json": {
                                            "schema": {
                                                "$ref": "#/components/schemas/Missing"
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                },
            },
            endpoint="https://api.example.test",
        )
