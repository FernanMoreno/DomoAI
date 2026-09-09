from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from domoai.application.connection_providers import (
    ConnectionProviderManifest,
    ConnectionSetup,
)
from domoai.domain.connections import ConnectionCandidate, ConnectionDraft, ConnectionProfile
from domoai.runtime.managed_adapter_set import ManagedAdapterSet
from domoai.security.secrets import FileSecretStore


def test_connection_contract_keeps_profile_metadata_secret_safe() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        ConnectionProfile(
            id=uuid4(),
            connector_id="fixture",
            unique_id="fixture:one",
            endpoint={"token": "must-not-be-here"},
            options={},
            created_at=now,
            updated_at=now,
        )


def test_provider_contract_requires_selected_candidate_provenance() -> None:
    candidate = ConnectionCandidate(
        connector_id="fixture",
        candidate_id="fixture:one",
        display_name="Fixture",
        endpoint={"host": "fixture"},
    )
    with pytest.raises(ValidationError):
        ConnectionSetup(
            candidate=candidate,
            draft=ConnectionDraft(
                connector_id="other",
                unique_id="fixture:one",
                endpoint={"host": "fixture"},
                options={},
            ),
        )


def test_provider_manifest_is_versioned_and_setup_steps_are_unique() -> None:
    manifest = ConnectionProviderManifest(
        connector_id="fixture",
        display_name="Fixture",
        protocol="fixture",
        version="1.0",
    )
    assert manifest.contract_version == "v1"


def test_empty_managed_runtime_is_an_adapter_boundary() -> None:
    runtime = ManagedAdapterSet()
    assert runtime.adapters == ()
    assert runtime.registry is None


def test_secret_store_returns_opaque_reference_and_supports_env_refs(tmp_path, monkeypatch) -> None:
    store = FileSecretStore(tmp_path / "master.key")
    reference = store.put("contract-secret")
    monkeypatch.setenv("DOMOAI_CONTRACT_SECRET", "environment-secret")

    assert reference.startswith("secret:")
    assert store.resolve(reference) == "contract-secret"
    assert store.resolve("env:DOMOAI_CONTRACT_SECRET") == "environment-secret"
    assert "contract-secret" not in repr(store)
