from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from tests.fixtures.deployment_preflight import write_deployment

from domoai.admin.deployment_preflight import run_preflight


@pytest.mark.asyncio
async def test_static_preflight_does_not_open_dependency_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _ = write_deployment(tmp_path)

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("static preflight must not perform network I/O")

    monkeypatch.setattr(asyncio, "open_connection", fail_if_called)

    report = await run_preflight(request)

    assert report.status == "passed"


@pytest.mark.asyncio
async def test_network_preflight_connects_only_when_explicitly_requested(
    tmp_path: Path,
) -> None:
    probe_server = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
    port = probe_server.sockets[0].getsockname()[1]
    request, _ = write_deployment(tmp_path)
    request.env_file.write_text(
        request.env_file.read_text(encoding="utf-8").replace(
            "http://homeassistant:8123", f"http://127.0.0.1:{port}"
        ).replace(
            "DOMOAI_ZIGBEE2MQTT_URL=mqtt://mqtt:1883\n", ""
        ).replace(
            "DOMOAI_KNX_GATEWAY_HOST=host.docker.internal\n", ""
        ).replace(
            "DOMOAI_KNX_GATEWAY_PORT=3672\n", ""
        ).replace(
            "DOMOAI_KNX_CONFIG_PATH=/app/config/knx.json\n", ""
        ).replace(
            f"DOMOAI_KNX_CONFIG_PATH_HOST={tmp_path / 'knx.json'}\n", ""
        ),
        encoding="utf-8",
    )
    request = replace(request, network=True)

    try:
        report = await run_preflight(request)
    finally:
        probe_server.close()
        await probe_server.wait_closed()

    assert report.status == "passed"
    assert any(check.name == "network_dependencies" for check in report.checks)


@pytest.mark.asyncio
async def test_network_failure_is_stable_and_does_not_include_exception_text(
    tmp_path: Path,
) -> None:
    request, _ = write_deployment(tmp_path)
    request.env_file.write_text(
        request.env_file.read_text(encoding="utf-8").replace(
            "http://homeassistant:8123", "http://127.0.0.1:1"
        ).replace(
            "DOMOAI_ZIGBEE2MQTT_URL=mqtt://mqtt:1883\n", ""
        ).replace(
            "DOMOAI_KNX_GATEWAY_HOST=host.docker.internal\n", ""
        ).replace(
            "DOMOAI_KNX_GATEWAY_PORT=3672\n", ""
        ).replace(
            "DOMOAI_KNX_CONFIG_PATH=/app/config/knx.json\n", ""
        ).replace(
            f"DOMOAI_KNX_CONFIG_PATH_HOST={tmp_path / 'knx.json'}\n", ""
        ),
        encoding="utf-8",
    )
    request = replace(request, network=True, timeout_seconds=0.2)

    report = await run_preflight(request)

    assert report.status == "failed"
    assert any(check.code == "dependency_unavailable" for check in report.checks)
    assert all("127.0.0.1" not in check.name for check in report.checks)
