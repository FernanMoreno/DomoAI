from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from domoai.application.runtime_bootstrap import RuntimeBootstrap
from domoai.config.settings import Settings


def test_lab_bootstrap_resolves_reachable_allowlisted_endpoints_and_writes_secret_free_manifest(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_path=tmp_path / "runtime.sqlite3",
        bootstrap_profile="lab",
        bootstrap_manifest_path=tmp_path / "bootstrap.json",
        home_assistant_token=SecretStr("do-not-persist-this-token"),
    )

    reachable = {
        ("127.0.0.1", 8123),
        ("127.0.0.1", 1883),
        ("127.0.0.1", 5580),
        ("127.0.0.1", 1502),
    }
    result = RuntimeBootstrap.resolve(
        settings,
        probe=lambda host, port: (host, port) in reachable,
        now="2026-08-31T12:00:00Z",
        project_root=Path.cwd(),
    )

    assert result.settings.home_assistant_url == "http://127.0.0.1:8123"
    assert result.settings.zigbee2mqtt_url == "mqtt://127.0.0.1:1883"
    assert result.settings.matter_server_url == "ws://127.0.0.1:5580/ws"
    assert result.settings.modbus_host == "127.0.0.1"
    assert result.settings.modbus_config_path == Path("dev/lab/configs/modbus.json")
    assert result.settings.battery_dispatch_profile_path == Path(
        "dev/lab/configs/dispatchable-battery-lab.json"
    )
    assert result.settings.ev_charging_binding_paths == (
        Path("dev/lab/configs/ev-charging-lab.json"),
    )
    assert result.manifest.profile == "lab"
    assert result.manifest_path == tmp_path / "bootstrap.json"
    document = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    encoded = json.dumps(document, sort_keys=True)
    assert "do-not-persist-this-token" not in encoded
    resolved = {
        item["provider_id"]: item["status"]
        for item in document["candidates"]
        if item["provider_id"] != "knx"
    }
    assert resolved == {
        "home_assistant": "auto_configured",
        "zigbee2mqtt": "auto_configured",
        "matter": "auto_configured",
        "modbus": "auto_configured",
    }
    home_assistant = next(
        item for item in document["candidates"] if item["provider_id"] == "home_assistant"
    )
    assert home_assistant["operational_paths"] == [
        "dev/lab/configs/dispatchable-battery-lab.json",
        "dev/lab/configs/ev-charging-lab.json",
    ]


def test_lab_bootstrap_does_not_select_actuator_assets_for_explicit_non_lab_endpoint(
    tmp_path: Path,
) -> None:
    settings = Settings(
        bootstrap_profile="lab",
        bootstrap_manifest_path=tmp_path / "bootstrap.json",
        database_path=tmp_path / "runtime.sqlite3",
        home_assistant_url="https://ha.example.test",
        home_assistant_token=SecretStr("do-not-persist-this-token"),
    )

    result = RuntimeBootstrap.resolve(
        settings,
        probe=lambda _host, _port: True,
        now="2026-08-31T12:00:00Z",
        project_root=Path.cwd(),
    )

    assert result.settings.battery_dispatch_profile_path is None
    assert result.settings.ev_charging_binding_paths == ()
    home_assistant = next(
        item for item in result.manifest.candidates if item.provider_id == "home_assistant"
    )
    assert home_assistant.operational_paths == []


def test_lab_bootstrap_preserves_explicit_operational_assets(tmp_path: Path) -> None:
    battery_path = tmp_path / "battery.json"
    ev_path = tmp_path / "ev.json"
    settings = Settings(
        bootstrap_profile="lab",
        bootstrap_manifest_path=tmp_path / "bootstrap.json",
        database_path=tmp_path / "runtime.sqlite3",
        home_assistant_token=SecretStr("do-not-persist-this-token"),
        battery_dispatch_profile_path=battery_path,
        ev_charging_binding_paths=(ev_path,),
    )

    result = RuntimeBootstrap.resolve(
        settings,
        probe=lambda _host, _port: True,
        now="2026-08-31T12:00:00Z",
        project_root=Path.cwd(),
    )

    assert result.settings.battery_dispatch_profile_path == battery_path
    assert result.settings.ev_charging_binding_paths == (ev_path,)
    home_assistant = next(
        item for item in result.manifest.candidates if item.provider_id == "home_assistant"
    )
    assert home_assistant.operational_paths == [str(battery_path), str(ev_path)]


def test_bootstrap_never_replaces_explicit_values(tmp_path: Path) -> None:
    settings = Settings(
        bootstrap_profile="lab",
        bootstrap_manifest_path=tmp_path / "bootstrap.json",
        zigbee2mqtt_url="mqtt://127.0.0.1:1884",
        database_path=tmp_path / "runtime.sqlite3",
    )

    result = RuntimeBootstrap.resolve(
        settings,
        probe=lambda _host, _port: True,
        now="2026-08-31T12:00:00Z",
        project_root=Path.cwd(),
    )

    assert result.settings.zigbee2mqtt_url == "mqtt://127.0.0.1:1884"
    z2m = next(item for item in result.manifest.candidates if item.provider_id == "zigbee2mqtt")
    assert z2m.status == "configured"
    assert z2m.reason_code == "explicit_configuration"


def test_default_bootstrap_profile_does_not_probe_or_write(tmp_path: Path) -> None:
    settings = Settings(database_path=tmp_path / "runtime.sqlite3")
    called = False

    def probe(_host: str, _port: int) -> bool:
        nonlocal called
        called = True
        return True

    result = RuntimeBootstrap.resolve(
        settings,
        probe=probe,
        now="2026-08-31T12:00:00Z",
        project_root=Path.cwd(),
    )

    assert called is False
    assert result.settings == settings
    assert result.manifest.profile == "none"
    assert result.manifest_path is None


def test_missing_home_assistant_credential_does_not_auto_enable_provider(tmp_path: Path) -> None:
    settings = Settings(
        bootstrap_profile="lab",
        bootstrap_manifest_path=tmp_path / "bootstrap.json",
        database_path=tmp_path / "runtime.sqlite3",
    )

    result = RuntimeBootstrap.resolve(
        settings,
        probe=lambda _host, _port: True,
        now="2026-08-31T12:00:00Z",
        project_root=Path.cwd(),
    )

    assert result.settings.home_assistant_url is None
    ha = next(item for item in result.manifest.candidates if item.provider_id == "home_assistant")
    assert ha.status == "skipped"
    assert ha.reason_code == "credentials_missing"


def test_lab_bootstrap_uses_the_separate_wsl_knx_gateway_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        bootstrap_profile="lab",
        bootstrap_manifest_path=tmp_path / "bootstrap.json",
        database_path=tmp_path / "runtime.sqlite3",
        knx_virtual_host="172.26.80.1",
    )
    monkeypatch.setattr(
        "domoai.application.runtime_bootstrap._derive_local_source_host",
        lambda _host: "172.26.93.253",
    )

    result = RuntimeBootstrap.resolve(
        settings,
        probe=lambda host, port: (host, port) == ("172.26.93.253", 3672),
        now="2026-08-31T12:00:00Z",
        project_root=Path.cwd(),
    )

    assert result.settings.knx_gateway_host == "172.26.93.253"
    assert result.settings.knx_gateway_port == 3672
    knx = next(item for item in result.manifest.candidates if item.provider_id == "knx")
    assert knx.endpoint == "udp://172.26.93.253:3672"
    assert knx.status == "auto_configured"


def test_unavailable_candidate_is_recorded_and_manifest_is_replaced_atomically(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "bootstrap.json"
    settings = Settings(
        bootstrap_profile="lab",
        bootstrap_manifest_path=manifest_path,
        database_path=tmp_path / "runtime.sqlite3",
    )
    manifest_path.write_text('{"old": true}', encoding="utf-8")

    result = RuntimeBootstrap.resolve(
        settings,
        probe=lambda _host, _port: False,
        now="2026-08-31T12:00:00Z",
        project_root=Path.cwd(),
    )

    assert result.settings.zigbee2mqtt_url is None
    assert next(
        item for item in result.manifest.candidates if item.provider_id == "zigbee2mqtt"
    ).reason_code == "endpoint_unreachable"
    assert '"old": true' not in manifest_path.read_text(encoding="utf-8")
    assert not list(tmp_path.glob(".bootstrap.json.*.tmp"))
