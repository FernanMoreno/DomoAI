from pathlib import Path

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.persistence.repositories import RuntimeOwnershipConflict


class _CountingAdapter(SimulatedHomeAdapter):
    def __init__(self, adapter_id: str) -> None:
        super().__init__()
        self.adapter_id = adapter_id
        self.connect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        await super().connect()


@pytest.mark.asyncio
async def test_only_one_runtime_can_own_a_deployment_before_adapter_connect(tmp_path: Path) -> None:
    database_path = tmp_path / "ownership.sqlite3"
    settings = Settings(database_path=database_path, mcp_deployment_id="home-main")
    first_adapter = _CountingAdapter("first")
    second_adapter = _CountingAdapter("second")

    first = await build_runtime(settings, adapter=first_adapter)
    try:
        with pytest.raises(RuntimeOwnershipConflict, match="active"):
            await build_runtime(settings, adapter=second_adapter)
        assert first_adapter.connect_calls == 1
        assert second_adapter.connect_calls == 0
    finally:
        await first.close()

    second = await build_runtime(settings, adapter=second_adapter)
    await second.close()
    assert second_adapter.connect_calls == 1
