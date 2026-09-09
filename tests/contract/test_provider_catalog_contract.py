from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from domoai.domain.connections import (
    ProviderCatalogEntry,
    SetupFieldDescriptor,
    SetupFieldType,
    SetupStep,
)
from domoai.runtime.connection_ports import ConnectionProviderManifest


def test_setup_field_descriptor_describes_a_secret_url_without_a_value() -> None:
    field = SetupFieldDescriptor(
        key="token",
        label="Access token",
        input_type=SetupFieldType.PASSWORD,
        secret=True,
        help_text="Paste the token created in the provider.",
    )

    assert field.model_dump(mode="json") == {
        "key": "token",
        "label": "Access token",
        "input_type": "password",
        "required": True,
        "secret": True,
        "help_text": "Paste the token created in the provider.",
        "options": [],
    }
    assert "token-value" not in field.model_dump_json()


def test_setup_step_requires_fields_to_match_declared_inputs_and_secret_keys() -> None:
    step = SetupStep(
        step_id="credentials",
        title="Credentials",
        input_keys=("token",),
        secret_input_keys=("token",),
        fields=(
            SetupFieldDescriptor(
                key="token",
                label="Access token",
                input_type=SetupFieldType.PASSWORD,
                secret=True,
            ),
        ),
    )

    assert step.fields[0].key == "token"

    with pytest.raises(ValidationError, match="fields must match"):
        SetupStep(
            step_id="credentials",
            title="Credentials",
            input_keys=("token",),
            secret_input_keys=("token",),
            fields=(
                SetupFieldDescriptor(
                    key="token",
                    label="Access token",
                    input_type=SetupFieldType.PASSWORD,
                    secret=False,
                ),
            ),
        )


def test_provider_manifest_projects_a_secret_free_catalog_entry() -> None:
    manifest = ConnectionProviderManifest(
        connector_id="fixture_catalog",
        display_name="Fixture Catalog",
        protocol="fixture",
        version="1.0",
        supports_discovery=False,
        supports_manual_setup=True,
        steps=(
            SetupStep(
                step_id="endpoint",
                title="Endpoint",
                input_keys=("url",),
                fields=(
                    SetupFieldDescriptor(
                        key="url",
                        label="Endpoint",
                        input_type=SetupFieldType.URL,
                    ),
                ),
            ),
        ),
    )
    entry = ProviderCatalogEntry.from_manifest(manifest)

    assert entry.connector_id == "fixture_catalog"
    assert entry.supports_discovery is False
    assert entry.supports_manual_setup is True
    assert "credential" not in entry.model_dump_json().casefold()


def test_catalog_and_setup_descriptor_schemas_are_published() -> None:
    root = Path(__file__).parents[2] / "schemas" / "v1"
    descriptor = json.loads(
        (root / "setup-field-descriptor.schema.json").read_text(encoding="utf-8")
    )
    catalog = json.loads(
        (root / "provider-catalog-entry.schema.json").read_text(encoding="utf-8")
    )

    assert descriptor["properties"]["input_type"]["$ref"].endswith("#/$defs/SetupFieldType")
    assert "supports_manual_setup" in catalog["properties"]
