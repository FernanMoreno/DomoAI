from pathlib import Path

import pytest

from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings


def test_settings_default_to_single_writer_and_expose_instance_identity() -> None:
    settings = Settings()

    assert settings.multi_host_enabled is False
    assert settings.instance_id is None
    assert settings.household_queue_max_per_household > 0


@pytest.mark.asyncio
async def test_multi_host_startup_fails_without_external_coordinator(tmp_path: Path) -> None:
    settings = Settings(
        multi_host_enabled=True,
        database_path=tmp_path / "runtime.sqlite3",
    )

    with pytest.raises(ValueError, match="external lease coordinator"):
        await build_runtime(settings)
