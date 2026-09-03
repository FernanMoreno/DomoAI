from __future__ import annotations

from pathlib import Path

import pytest

from domoai.config.settings import Settings


def test_bootstrap_settings_are_loaded_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOMOAI_BOOTSTRAP_PROFILE", "lab")
    monkeypatch.setenv("DOMOAI_BOOTSTRAP_MANIFEST_PATH", "data/bootstrap.json")
    monkeypatch.setenv("DOMOAI_KNX_KV_HOST", "172.26.80.1")

    settings = Settings.from_environment()

    assert settings.bootstrap_profile == "lab"
    assert settings.bootstrap_manifest_path == Path("data/bootstrap.json")
    assert settings.knx_virtual_host == "172.26.80.1"


def test_unknown_bootstrap_profile_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOMOAI_BOOTSTRAP_PROFILE", "network-scan")

    with pytest.raises(ValueError, match="DOMOAI_BOOTSTRAP_PROFILE"):
        Settings.from_environment()
