import pytest


class _CloseRecorder:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events

    async def close(self) -> None:
        self.events.append(self.name)


class _Lifecycle:
    async def close(self) -> None:
        return None


class _Adapter:
    async def disconnect(self) -> None:
        return None


class _Ownership:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def release(self) -> bool:
        self.events.append("ownership")
        return True


@pytest.mark.asyncio
async def test_runtime_releases_physical_ownership_before_storage_shutdown() -> None:
    from domoai.application.runtime_factory import RuntimeComposition

    events: list[str] = []
    runtime = RuntimeComposition.__new__(RuntimeComposition)
    runtime.lifecycle = _Lifecycle()
    runtime.battery_control_coordinator = None
    runtime.adapter = _Adapter()
    runtime.blocking_workers = []
    runtime.energy_closers = ()
    runtime.ownership = _Ownership(events)
    runtime.storage = _CloseRecorder("storage", events)
    runtime.audit_storage = _CloseRecorder("audit_storage", events)
    runtime.database = _CloseRecorder("database", events)
    runtime.approval_database = _CloseRecorder("approval_database", events)
    runtime.audit_database = _CloseRecorder("audit_database", events)

    await runtime.close()

    assert events.index("ownership") < events.index("storage")
    assert events.index("ownership") < events.index("audit_storage")
