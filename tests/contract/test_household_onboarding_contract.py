"""Versioned household projections preserve strict legacy contracts."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.fixtures.household_onboarding import onboarding_gateway


@pytest.mark.asyncio
@pytest.mark.parametrize("device_id", ["status", "inventory", "ordinary-missing"])
async def test_device_identifiers_never_dispatch_collection_routes(tmp_path, device_id):
    async with onboarding_gateway(tmp_path) as gateway:
        async with gateway.http_client() as client:
            response = await client.get(f"/api/v1/onboarding/state/{device_id}")
            invalid = await client.get(f"/api/v1/onboarding/state/{device_id}?limit=2")
    assert response.status_code == 404
    assert response.json() == {"error": "not_found"}
    assert invalid.status_code == 400


@pytest.mark.asyncio
async def test_empty_runtime_exposes_strict_status_and_inventory_without_secrets(tmp_path):
    async with onboarding_gateway(tmp_path) as gateway:
        async with gateway.http_client() as client:
            status = await client.get("/api/v1/onboarding/status")
            inventory = await client.get("/api/v1/onboarding/inventory")
        assert status.status_code == 200
        assert inventory.status_code == 200
        from domoai.domain.household_onboarding import (
            HouseholdOnboardingInventory,
            HouseholdOnboardingStatus,
        )

        parsed = HouseholdOnboardingStatus.model_validate(status.json())
        assert parsed.schema_version == "domoai.household-onboarding.v1"
        assert parsed.auth_mode == "local_loopback"
        assert parsed.can_manage_connections is True
        visible = HouseholdOnboardingInventory.model_validate(inventory.json())
        assert visible.devices == []
        assert visible.coverage_evidence.rows == []
        for model, payload in [
            (HouseholdOnboardingStatus, status.json()),
            (HouseholdOnboardingInventory, inventory.json()),
        ]:
            with pytest.raises(ValidationError):
                model.model_validate({**payload, "authority": {"roles": ["owner"]}})
        combined = status.text + inventory.text
        assert "credential_ref" not in combined and "secret:" not in combined
        assert str(tmp_path) not in combined


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/onboarding/status?path=/private",
        "/api/v1/onboarding/status?host=external.test",
        "/api/v1/onboarding/status?token=secret",
        "/api/v1/onboarding/inventory?limit=101",
        "/api/v1/onboarding/inventory?limit=0",
        "/api/v1/onboarding/inventory?limit=abc",
        "/api/v1/onboarding/inventory?cursor=invalid",
        "/api/v1/onboarding/state/missing?connection_id=invalid",
        "/api/v1/onboarding/state/missing?authority=owner",
    ],
)
async def test_projection_rejects_unbounded_or_operational_parameters(tmp_path, path):
    async with onboarding_gateway(tmp_path) as gateway:
        async with gateway.http_client() as client:
            response = await client.get(path)
        assert response.status_code == 400
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_new_projection_exports_match_models_without_replacing_legacy(tmp_path):
    async with onboarding_gateway(tmp_path) as gateway:
        async with gateway.http_client() as client:
            response = await client.get("/api/v1/onboarding/status")
        assert response.status_code == 200
    from domoai.domain.household_onboarding import (
        HouseholdOnboardingInventory,
        HouseholdOnboardingRead,
        HouseholdOnboardingStatus,
    )

    schemas = Path(__file__).resolve().parents[2] / "schemas" / "v1"
    for suffix, model in [
        ("status", HouseholdOnboardingStatus),
        ("inventory", HouseholdOnboardingInventory),
        ("read", HouseholdOnboardingRead),
    ]:
        schema = json.loads((schemas / f"household-onboarding-{suffix}.schema.json").read_text())
        assert schema == model.model_json_schema(by_alias=True)
        assert schema["additionalProperties"] is False
    from domoai.domain.connections import ConnectionDraft

    with pytest.raises(ValidationError):
        ConnectionDraft.model_validate(
            {
                "connector_id": "fixture",
                "unique_id": "fixture",
                "endpoint": {},
                "read_status": "verified",
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "discarding"])
@pytest.mark.parametrize("foreign", [False, True])
async def test_http_status_reports_scoped_orphan_recovery_after_restart_without_effects(
    tmp_path, state, foreign
):
    from uuid import uuid4

    from domoai.domain.coordination import LeaseScope
    from domoai.persistence.connection_repository import ConnectionRepository, SecretTransfer
    from domoai.persistence.serialized import SerializedRepositoryProxy

    async with onboarding_gateway(tmp_path) as gateway:
        runtime = gateway.runtime
        store = runtime.connection_manager.secret_store
        owner = object()
        reference = store.put_temporary("private-http-orphan", owner=owner)
        prepared = store.prepare_temporary(reference, owner=owner)
        transfer = SecretTransfer(uuid4(), reference, uuid4(), "fixture", "orphan", prepared.digest)
        scope = LeaseScope(
            tenant_id="tenant-a",
            household_id="foreign" if foreign else "home-a",
            deployment_id="edge",
        )
        repository = SerializedRepositoryProxy(
            ConnectionRepository(runtime.database, scope=scope), runtime.storage
        )
        await repository.prepare_secret_transfer(transfer)
        store.materialize(prepared)
        if state == "discarding":
            assert await repository.begin_secret_discard(transfer)
        before = {path.name: path.read_bytes() for path in store.secret_dir.glob("*.json")}
    async with onboarding_gateway(tmp_path) as gateway:
        async with gateway.http_client() as client:
            response = await client.get("/api/v1/onboarding/status")
            profiles = await client.get("/api/v1/connections")
        assert response.status_code == 200 and profiles.status_code == 200
        assert response.json()["recovery_diagnostics"] == (
            [] if foreign else ["connection_recovery_pending"]
        )
        assert {item["code"] for item in response.json()["checks"]} == {
            "service",
            "lifecycle",
            "ownership",
            "connectors",
        }
        assert "private-http-orphan" not in response.text
        assert reference not in response.text and str(transfer.profile_id) not in response.text
        assert gateway.runtime.adapter.adapters == ()
        assert {path.name: path.read_bytes() for path in store.secret_dir.glob("*.json")} == before
        repository = SerializedRepositoryProxy(
            ConnectionRepository(gateway.runtime.database, scope=scope), gateway.runtime.storage
        )
        assert (await repository.get_secret_transfer(transfer.transfer_id)).state == state


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_status_recovery_query_failure_is_sanitized_and_cancellation_propagates(
    tmp_path, cancel, monkeypatch
):
    import asyncio

    from domoai.application.authority import AuthorityPolicy
    from domoai.application.household_onboarding import HouseholdOnboardingService
    from domoai.application.state_service import StateService
    from domoai.mcp.resources import coverage_evidence_snapshot

    async with onboarding_gateway(tmp_path) as gateway:
        runtime = gateway.runtime
        manager = runtime.connection_manager

        async def unavailable():
            if cancel:
                raise asyncio.CancelledError
            raise RuntimeError("private-storage-query")

        monkeypatch.setattr(manager.repository, "has_unresolved_secret_transfers", unavailable)
        if cancel:
            policy = AuthorityPolicy(tenant_id="tenant-a", household_id="home-a")
            service = HouseholdOnboardingService(
                registry=runtime.registry,
                state_service=StateService(runtime.state_store),
                manager=manager,
                authority_policy=policy,
                coverage_projector=coverage_evidence_snapshot,
                membership=lambda: (),
                readiness=lambda: [],
                public_url="http://localhost/mcp",
            )
            with pytest.raises(asyncio.CancelledError):
                await service.status(
                    policy.local_context(), auth_mode="local_loopback", manage_scope=True
                )
        else:
            async with gateway.http_client() as client:
                response = await client.get("/api/v1/onboarding/status")
            assert response.status_code == 200
            assert response.json()["recovery_diagnostics"] == ["connection_persistence_pending"]
            assert "private-storage-query" not in response.text
        assert runtime.adapter.adapters == ()


@pytest.mark.asyncio
async def test_status_with_adopted_profile_never_reads_secret_files(tmp_path, monkeypatch):
    from tests.fixtures.household_onboarding import (
        connect_home_assistant,
        home_assistant_http_source,
    )

    async with home_assistant_http_source() as source, onboarding_gateway(tmp_path) as gateway:
        await connect_home_assistant(gateway, source)
        await gateway.runtime.lifecycle.close()
        store = gateway.runtime.connection_manager.secret_store
        calls = []

        def forbidden_filesystem(*_args, **_kwargs):
            calls.append("secret-filesystem")
            raise AssertionError("status must only query operational metadata")

        monkeypatch.setattr(store, "matches_secret", forbidden_filesystem)
        monkeypatch.setattr(store, "resolve", forbidden_filesystem)
        async with gateway.http_client() as client:
            response = await client.get("/api/v1/onboarding/status")
        assert calls == []
        assert response.status_code == 200
        assert response.json()["recovery_diagnostics"] == []
        assert source.commands == 0
