from __future__ import annotations

import pytest

from domoai.config.settings import Settings
from domoai.mcp.stdio import require_live_deployment_source


def test_stdio_launcher_rejects_unconfigured_runtime_instead_of_selecting_fixture() -> None:
    with pytest.raises(ValueError, match="no live adapter source"):
        require_live_deployment_source(Settings())


def test_stdio_launcher_accepts_explicit_home_assistant_source() -> None:
    require_live_deployment_source(
        Settings(home_assistant_url="http://home-assistant.test", home_assistant_token="token")
    )
