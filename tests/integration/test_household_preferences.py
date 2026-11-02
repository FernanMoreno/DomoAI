"""Authority, clock and live inventory composed with real profile persistence."""

import asyncio
import sqlite3
import threading

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.authority import AuthorityPolicy
from domoai.application.household_preferences import HouseholdPreferenceService
from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.domain.errors import DomainError
from domoai.domain.household_preferences import (
    PreferenceHistoryRead,
    PreferenceMutation,
    PreferenceRead,
    PreferenceRestore,
    PreferenceSlotMutation,
)
from domoai.persistence.household_preferences import HouseholdPreferenceRepository
from domoai.persistence.serialized import (
    SerializedRepositoryProxy,
    SerializedStorageExecutor,
    StorageOperationTimeoutError,
)
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.registry import DeviceRegistry
from tests.fixtures.household_preferences import authority, clock, devices, mutation, preference


@pytest.mark.asyncio
async def test_preference_conflicts_release_storage_admission_slots(direct_service):
    from uuid import uuid4

    service = direct_service
    raw_repository = service.repository
    storage = SerializedStorageExecutor(queue_capacity=1, queue_wait_seconds=0.01)
    service.repository = SerializedRepositoryProxy(raw_repository, storage)
    try:
        request = PreferenceMutation.model_validate(
            mutation({"generation": str(uuid4()), "revision": 0})
        )
        for _ in range(2):
            with pytest.raises(DomainError) as conflict:
                await service.set(request, authority())
            assert conflict.value.code == "preference_conflict"
        assert storage.metrics.queue_depth == 0
        profile = await service.read(PreferenceRead(), authority())
        assert profile.version.revision == 0
    finally:
        await storage.close()
        service.repository = raw_repository


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["set", "restore"])
@pytest.mark.parametrize("cancel_during_drain", [False, True])
async def test_timed_out_preference_commit_keeps_inventory_owner_until_terminal(
    tmp_path,
    operation,
    cancel_during_drain,
):
    fixed = clock()
    database_path = tmp_path / "timeout.sqlite"
    runtime = await build_runtime(
        Settings(database_path=database_path, mcp_tenant_id="tenant-a", mcp_household_id="home-a"),
        adapter=SimulatedHomeAdapter(clock=fixed),
        clock=fixed,
    )
    service = runtime.household_preferences
    runtime.registry.load_persisted(devices())
    entered = threading.Event()
    release = threading.Event()
    removal_attempted = asyncio.Event()
    revision_at_removal = []
    writer = remover = None

    def pause_commit():
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError("test did not release the SQLite worker")
        return 1

    async def remove_target():
        removal_attempted.set()
        async with service.inventory_lock:
            target = runtime.registry.get("water-heater")
            runtime.registry._remove_source_refs(target.id, target.source_refs)
            with sqlite3.connect(database_path) as observer:
                revision_at_removal.append(
                    observer.execute(
                        "SELECT revision FROM household_preference_heads "
                        "WHERE household_id='home-a'"
                    ).fetchone()[0]
                )

    try:
        initial = await service.read(PreferenceRead(), authority())
        request = PreferenceMutation.model_validate(
            mutation(initial.version.model_dump(mode="json"))
        )
        expected_revision = 1
        if operation == "restore":
            first = await service.set(request, authority())
            request = PreferenceRestore(
                expected_version=first.version,
                preference_id=first.preference.preference_id,
                idempotency_key="restore",
                source_revision=1,
                valid_from=fixed.now(),
                valid_until=None,
                timezone="UTC",
                provenance={"source_kind": "manual"},
            )
            expected_revision = 2
        runtime.database.connection.create_function("pause_preference_commit", 0, pause_commit)
        runtime.database.connection.execute(
            "CREATE TRIGGER pause_preference BEFORE UPDATE ON household_preference_heads "
            "BEGIN SELECT pause_preference_commit(); END"
        )
        runtime.database.connection.commit()
        runtime.storage.operation_timeout_seconds = 0.03
        writer = asyncio.create_task(getattr(service, operation)(request, authority()))
        assert await asyncio.to_thread(entered.wait, 2)
        async with asyncio.timeout(2):
            while runtime.storage.metrics.timeout_count == 0:
                await asyncio.sleep(0.001)
        if cancel_during_drain:
            writer.cancel()
            await asyncio.sleep(0)
            writer.cancel()
            await asyncio.sleep(0)
        remover = asyncio.create_task(remove_target())
        await removal_attempted.wait()
        release.set()
        outcome, _ = await asyncio.gather(writer, remover, return_exceptions=True)
        expected_error = (
            asyncio.CancelledError if cancel_during_drain else StorageOperationTimeoutError
        )
        assert isinstance(outcome, expected_error)
        assert runtime.registry.get("water-heater") is None
        # An independent reader at the actual removal boundary must already
        # observe the final commit. The pre-fix timeout allowed revision N-1.
        assert revision_at_removal == [expected_revision]
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (writer, remover) if task is not None), return_exceptions=True
        )
        runtime.storage.operation_timeout_seconds = 5
        await runtime.close()


@pytest.fixture
async def direct_service(tmp_path):
    database = SQLiteDatabase(tmp_path / "direct.sqlite")
    await database.initialize()
    repository = HouseholdPreferenceRepository(
        database, tenant_id="tenant-a", household_id="home-a"
    )
    await repository.bootstrap()
    registry = DeviceRegistry()
    registry.load_persisted(devices())
    service = HouseholdPreferenceService(
        repository,
        policy=AuthorityPolicy(tenant_id="tenant-a", household_id="home-a"),
        registry=registry,
        clock=clock(),
        inventory_lock=asyncio.Lock(),
    )
    try:
        yield service
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_confirmed_retry_after_expiry_and_erase_is_redacted(direct_service):
    service = direct_service
    empty = await service.read(PreferenceRead(), authority())
    request = PreferenceMutation.model_validate(mutation(empty.version.model_dump(mode="json")))
    confirmed = await service.set(request, authority())
    service.clock.set(request.preference.valid_until)
    replay = await service.set(request, authority())
    assert replay == confirmed
    erased = await service.erase(
        PreferenceSlotMutation(
            expected_version=confirmed.version,
            preference_id=confirmed.preference.preference_id,
            idempotency_key="erase",
        ),
        authority(),
    )
    replay = await service.set(request, authority())
    assert replay.preference is None and replay.version == confirmed.version
    assert (await service.read(PreferenceRead(), authority())).version == erased.version


@pytest.mark.asyncio
async def test_read_resolves_the_same_unique_alias_as_set_without_mutation(direct_service):
    service = direct_service
    service.registry = DeviceRegistry()
    service.registry.load_persisted(devices()[:1])
    initial = await service.read(PreferenceRead(), authority())
    alias = {"kind": "device", "alias": "Water heater"}
    saved = await service.set(
        PreferenceMutation.model_validate(
            mutation(
                initial.version.model_dump(mode="json"),
                preference=preference(target=alias),
            )
        ),
        authority(),
    )
    changes = service.repository.database.connection.total_changes
    by_alias = await service.read(PreferenceRead(target=alias), authority())
    by_id = await service.read(
        PreferenceRead(target={"kind": "device", "id": "water-heater"}),
        authority(),
    )
    assert len(by_alias.preferences) == 1
    assert by_alias.preferences == by_id.preferences
    assert by_alias.preferences[0].slot.preference_id == saved.preference.preference_id
    assert by_alias.version == saved.version
    assert service.repository.database.connection.total_changes == changes
    with pytest.raises(DomainError) as denied:
        await service.read(PreferenceRead(target=alias), authority(household="home-b"))
    assert denied.value.code == "insufficient_scope"


@pytest.mark.asyncio
async def test_ambiguity_and_inventory_changed_while_waiting_have_no_write(direct_service):
    service = direct_service
    empty = await service.read(PreferenceRead(), authority())
    ambiguous = PreferenceMutation.model_validate(
        mutation(
            empty.version.model_dump(mode="json"),
            preference=preference(target={"kind": "device", "alias": "Water heater"}),
        )
    )
    with pytest.raises(DomainError) as resolution:
        await service.set(ambiguous, authority())
    assert resolution.value.code == "resolution_required"
    assert len(resolution.value.details["candidates"]) == 2
    assert (await service.read(PreferenceRead(), authority())).version == empty.version
    await service.inventory_lock.acquire()
    pending = asyncio.create_task(
        service.set(
            PreferenceMutation.model_validate(mutation(empty.version.model_dump(mode="json"))),
            authority(),
        )
    )
    await asyncio.sleep(0)
    service.registry = DeviceRegistry()  # inventory owner removes target before allowing commit
    service.inventory_lock.release()
    with pytest.raises(DomainError) as changed:
        await pending
    assert changed.value.code == "unsupported_binding"
    assert (await service.history(PreferenceHistoryRead(), authority())).entries == []


@pytest.mark.asyncio
async def test_history_restore_acl_and_new_validity(direct_service):
    service = direct_service
    empty = await service.read(PreferenceRead(), authority())
    confirmed = await service.set(
        PreferenceMutation.model_validate(mutation(empty.version.model_dump(mode="json"))),
        authority(),
    )
    request = PreferenceRestore(
        expected_version=confirmed.version,
        preference_id=confirmed.preference.preference_id,
        idempotency_key="restore",
        source_revision=1,
        valid_from="2026-09-11T22:00:00+02:00",
        valid_until=None,
        timezone="Europe/Madrid",
        provenance={"source_kind": "manual"},
    )
    with pytest.raises(DomainError) as denied:
        await service.restore(request, authority(operations=["restore_household_preference"]))
    assert denied.value.code == "insufficient_scope"
    restored = await service.restore(request, authority())
    assert restored.version.revision == 2
    assert (await service.read(PreferenceRead(), authority())).preferences == []
    assert (await service.read(PreferenceRead(effective_only=False), authority())).preferences[
        0
    ].state == "scheduled"


@pytest.mark.asyncio
async def test_shared_service_starts_empty_without_read_mutation_and_expires(tmp_path):
    fixed = clock()
    settings = Settings(
        database_path=tmp_path / "home.sqlite", mcp_tenant_id="tenant-a", mcp_household_id="home-a"
    )
    runtime = await build_runtime(settings, adapter=SimulatedHomeAdapter(clock=fixed), clock=fixed)
    try:
        service = getattr(runtime, "household_preferences", None)
        assert service is not None, "runtime does not serve household preferences"
        profile = await service.read(PreferenceRead(), authority())
        assert profile.version.revision == 0 and profile.preferences == []
        device = next(
            d for d in runtime.registry.devices if any(c.writable for c in d.capabilities)
        )
        request = PreferenceMutation.model_validate(
            mutation(
                profile.version.model_dump(mode="json"),
                preference=preference(target={"kind": "device", "id": device.id}),
            )
        )
        result = await service.set(request, authority())
        shared = await service.read(PreferenceRead(), authority("resident-2"))
        assert shared.version == result.version and len(shared.preferences) == 1
        fixed.set(request.preference.valid_until)
        expired = await service.read(PreferenceRead(), authority())
        assert expired.preferences == [] and expired.version == result.version
        assert (
            len((await service.read(PreferenceRead(effective_only=False), authority())).preferences)
            == 1
        )
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("percent", [0, 25, 100])
async def test_soc_needs_typed_capacity_binding_and_preserves_percent(direct_service, percent):
    from domoai.domain.models import Capability, CapabilityKind
    from tests.unit.optimizer.test_energy_providers import _complete_dispatchable_binding

    service = direct_service
    battery = devices()[0].model_copy(
        update={
            "id": "battery.home",
            "capabilities": [
                Capability(
                    name="battery.soc",
                    kind=CapabilityKind.NUMBER,
                    unit="percent",
                    readable=True,
                    writable=False,
                )
            ],
        }
    )
    service.registry.load_persisted([battery])
    profile = await service.read(PreferenceRead(), authority())
    request = PreferenceMutation.model_validate(
        mutation(
            profile.version.model_dump(mode="json"),
            preference=preference(
                key="energy.minimum_soc",
                value=percent,
                unit="percent",
                target={"kind": "device", "id": "battery.home"},
            ),
        )
    )
    with pytest.raises(DomainError) as missing:
        await service.set(request, authority())
    assert missing.value.code == "unsupported_binding"
    service.battery_binding = _complete_dispatchable_binding()
    result = await service.set(request, authority())
    assert result.preference.preference.value == percent
    assert result.preference.preference.unit == "percent"


@pytest.mark.asyncio
async def test_pagination_binding_ambiguity_and_restricted_history(tmp_path):
    fixed = clock()
    runtime = await build_runtime(
        Settings(
            database_path=tmp_path / "home.sqlite",
            mcp_tenant_id="tenant-a",
            mcp_household_id="home-a",
        ),
        adapter=SimulatedHomeAdapter(clock=fixed),
        clock=fixed,
    )
    try:
        service = runtime.household_preferences
        profile = await service.read(PreferenceRead(), authority())
        devices = [d for d in runtime.registry.devices if any(c.writable for c in d.capabilities)][
            :2
        ]
        for index, device in enumerate(devices):
            confirmed = await service.set(
                PreferenceMutation.model_validate(
                    mutation(
                        profile.version.model_dump(mode="json"),
                        idempotency_key=f"set-{index}",
                        preference=preference(target={"kind": "device", "id": device.id}),
                    )
                ),
                authority(),
            )
            profile = await service.read(PreferenceRead(), authority())
        assert len(devices) == 2
        page = await service.read(PreferenceRead(limit=1), authority())
        assert page.next_cursor is not None
        next_page = await service.read(
            PreferenceRead(limit=1, cursor=page.next_cursor), authority()
        )
        assert next_page.preferences[0] != page.preferences[0]
        assert next_page.observed_at == page.observed_at
        with pytest.raises(DomainError) as cross_actor:
            await service.read(
                PreferenceRead(limit=1, cursor=page.next_cursor), authority("resident-2")
            )
        assert cross_actor.value.code == "stale_cursor"
        fixed.set(confirmed.preference.preference.valid_until)
        with pytest.raises(DomainError) as expired:
            await service.read(PreferenceRead(limit=1, cursor=page.next_cursor), authority())
        assert expired.value.code == "stale_cursor"
        history = await service.history(PreferenceHistoryRead(limit=1), authority())
        assert len(history.entries) == 1 and history.next_cursor
        with pytest.raises(DomainError) as denied:
            await service.history(PreferenceHistoryRead(), authority(role="operator"))
        assert denied.value.code == "insufficient_scope"
    finally:
        await runtime.close()
