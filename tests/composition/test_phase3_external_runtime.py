from pathlib import Path

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.coordination import DeterministicLeaseCoordinator
from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings


@pytest.mark.asyncio
async def test_multi_host_rejects_adapter_without_fencing_capability(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="fencing-aware adapter"):
        await build_runtime(
            Settings(multi_host_enabled=True, database_path=tmp_path / "runtime.sqlite3"),
            adapter=SimulatedHomeAdapter(),
            lease_coordinator=DeterministicLeaseCoordinator(),
        )
