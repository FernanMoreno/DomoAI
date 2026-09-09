from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from domoai.application.connection_manager import ConnectionManager, ConnectionManagerError
from domoai.application.connection_providers import (
    ConnectionProviderManifest,
    ConnectionProviderRegistry,
    ConnectionSetup,
    FixtureConnectionProvider,
)
from domoai.domain.connections import ConnectionCandidate, ConnectionDraft
from domoai.domain.models import (
    AdapterExecutionAck,
    AdapterHealth,
    AdapterSnapshot,
    Command,
    SourceEvent,
    SourceRef,
    StateSnapshot,
)
from domoai.persistence.connection_repository import ConnectionRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.managed_adapter_set import ManagedAdapterSet
from domoai.runtime.operational_metrics import RuntimeOperationalMetrics
from domoai.security.secrets import FileSecretStore


class FakeAdapter:
    adapter_id = "fixture:unset"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.connected = False

    async def connect(self) -> None:
        if self.fail:
            raise ConnectionError("vendor token must not leak")
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def discover(self) -> AdapterSnapshot:
        return AdapterSnapshot()

    async def read_state(self, source_refs: Sequence[SourceRef]) -> list[StateSnapshot]:
        return []

    async def execute(
        self, command: Command, execution_context: object | None = None
    ) -> AdapterExecutionAck:
        return AdapterExecutionAck(accepted=False)

    def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        async def events() -> AsyncIterator[SourceEvent]:
            if False:
                yield SourceEvent  # type: ignore[misc]

        return events()

    async def health(self) -> AdapterHealth:
        return AdapterHealth(adapter_id=self.adapter_id, connected=self.connected)


class AuditRecorder:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def append(
        self, *, event_type: str, actor: str, subject_id: str, payload: dict[str, object]
    ) -> None:
        self.events.append(
            {"event_type": event_type, "actor": actor, "subject_id": subject_id, **payload}
        )


def _setup() -> ConnectionSetup:
    candidate = ConnectionCandidate(
        connector_id="fixture",
        candidate_id="fixture:default",
        display_name="Fixture",
        endpoint={"host": "fixture"},
    )
    return ConnectionSetup(
        candidate=candidate,
        draft=ConnectionDraft(
            connector_id="fixture",
            unique_id="fixture:default",
            endpoint={"host": "fixture"},
            options={},
        ),
    )


async def _build_manager(
    tmp_path: Path,
    *,
    fail: bool = False,
    operational_metrics: RuntimeOperationalMetrics | None = None,
) -> tuple[ConnectionManager, SQLiteDatabase, AuditRecorder, FileSecretStore]:
    database = SQLiteDatabase(tmp_path / "manager.sqlite3")
    await database.initialize()
    repository = ConnectionRepository(database)
    runtime = ManagedAdapterSet()
    registry = ConnectionProviderRegistry(operational_metrics=operational_metrics)
    registry.register(
        FixtureConnectionProvider(
            ConnectionProviderManifest(
                connector_id="fixture",
                display_name="Fixture",
                protocol="fixture",
                version="1.0",
            ),
            adapter_factory=lambda: FakeAdapter(fail=fail),
        )
    )
    audit = AuditRecorder()
    secret_store = FileSecretStore(tmp_path / "master.key")
    return (
        ConnectionManager(
            repository=repository,
            providers=registry,
            runtime=runtime,
            secret_store=secret_store,
            audit=audit,
        ),
        database,
        audit,
        secret_store,
    )


@pytest.mark.asyncio
async def test_create_persists_opaque_credential_before_activation(tmp_path: Path) -> None:
    manager, database, audit, secret_store = await _build_manager(tmp_path)
    try:
        profile = await manager.create(_setup(), credential_value="vendor-token-plaintext")

        assert profile.enabled
        assert profile.credential_ref is not None
        assert profile.credential_ref.startswith("secret:")
        assert secret_store.resolve(profile.credential_ref) == "vendor-token-plaintext"
        row = database.connection.execute(
            "SELECT credential_ref, endpoint_json, options_json FROM connections"
        ).fetchone()
        assert row is not None
        assert "vendor-token-plaintext" not in str(tuple(row))
        assert audit.events[-1]["event_type"] == "connection_activated"
    finally:
        await manager.runtime.disconnect()
        await database.close()


@pytest.mark.asyncio
async def test_failed_activation_compensates_persisted_profile(tmp_path: Path) -> None:
    manager, database, audit, _ = await _build_manager(tmp_path, fail=True)
    try:
        with pytest.raises(ConnectionManagerError, match="connection activation failed") as exc:
            await manager.create(_setup(), credential_value="vendor-token-plaintext")
        assert exc.value.code == "activation_failed"
        profiles = await manager.repository.list_all()
        assert len(profiles) == 1
        assert not profiles[0].enabled
        assert "vendor-token-plaintext" not in repr(audit.events)
        assert audit.events[-1]["event_type"] == "connection_activation_failed"
    finally:
        await manager.runtime.disconnect()
        await database.close()


@pytest.mark.asyncio
async def test_disable_and_delete_are_idempotent_and_audited(tmp_path: Path) -> None:
    manager, database, _audit, _ = await _build_manager(tmp_path)
    try:
        profile = await manager.create(_setup())
        assert await manager.disable(profile.id)
        assert not await manager.disable(profile.id)
        assert await manager.delete(profile.id)
        assert not await manager.delete(profile.id)
        assert await manager.repository.get(profile.id) is None
        assert manager.runtime.adapters == ()
    finally:
        await manager.runtime.disconnect()
        await database.close()


@pytest.mark.asyncio
async def test_reconfigure_preserves_connection_identity_and_replaces_runtime_child(
    tmp_path: Path,
) -> None:
    manager, database, audit, _ = await _build_manager(tmp_path)
    try:
        profile = await manager.create(_setup())
        updated = await manager.reconfigure(
            profile.id,
            endpoint={"host": "fixture-reconfigured"},
            options={"profile": "v1"},
        )

        assert updated.id == profile.id
        assert updated.endpoint == {"host": "fixture-reconfigured"}
        assert manager.runtime.adapters[0].adapter_id == f"connection:{profile.id}"
        assert audit.events[-1]["event_type"] == "connection_reconfigured"
    finally:
        await manager.runtime.disconnect()
        await database.close()


@pytest.mark.asyncio
async def test_manager_onboarding_session_reaches_persisted_profile(tmp_path: Path) -> None:
    manager, database, _audit, _secret_store = await _build_manager(tmp_path)
    try:
        session_id, candidates = await manager.start_setup("fixture")
        assert candidates[0].candidate_id == "fixture:default"
        await manager.select_setup(session_id, "fixture:default")
        await manager.setup_draft(
            session_id,
            endpoint={"host": "fixture"},
            options={},
        )
        validation = await manager.validate_setup(session_id)
        profile = await manager.create_setup(session_id, activate=False)

        assert validation.valid is True
        assert profile.enabled is False
        assert await manager.repository.get(profile.id) == profile
    finally:
        await manager.runtime.disconnect()
        await database.close()


@pytest.mark.asyncio
async def test_manager_records_activation_and_replacement_connection_metrics(
    tmp_path: Path,
) -> None:
    metrics = RuntimeOperationalMetrics()
    manager, database, _audit, _secret_store = await _build_manager(
        tmp_path, operational_metrics=metrics
    )
    try:
        profile = await manager.create(_setup())
        await manager.replace(profile.id)

        connections = metrics.snapshot()["connections"]
        assert connections["connection_connected"] == 2
        assert connections["connection_reconnect_attempt_total"] == 1
        assert connections["connection_reconnect_success_total"] == 1
    finally:
        await manager.runtime.disconnect()
        await database.close()
