"""Tooling gate for the P01 browser harness; product journeys begin in T013."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from domoai.management.api import create_management_app

_BROWSER_DIR = Path(__file__).parent
_COMPANION = _BROWSER_DIR / "run_household_onboarding_browser.cjs"
_FIXTURE_BEARER = "p01-t003-browser-fixture-not-a-user-token"


def _wait_until_started(server: uvicorn.Server, thread: threading.Thread) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if server.started:
            return
        if not thread.is_alive():
            raise RuntimeError("disposable management HTTP server exited before startup")
        time.sleep(0.02)
    raise TimeoutError("disposable management HTTP server did not start in 10 seconds")


@pytest.fixture
def management_http_server() -> str:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = int(listener.getsockname()[1])
    app = create_management_app(
        object(),
        host="127.0.0.1",
        auth_token=_FIXTURE_BEARER,
        rate_limit=1_000,
    )
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        _wait_until_started(server, thread)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        if thread.is_alive():
            raise RuntimeError("disposable management HTTP server did not stop")


def test_browser_harness_opens_current_management_app_over_loopback_http(
    management_http_server: str, tmp_path: Path
) -> None:
    """Prove only the real HTTP + Chromium toolchain required by T003."""

    assert os.environ.get("PLAYWRIGHT_BROWSERS_PATH"), (
        "set PLAYWRIGHT_BROWSERS_PATH to an explicitly provisioned local browser directory"
    )
    evidence_path = tmp_path / "browser-tooling-evidence.json"
    environment = {
        **os.environ,
        "P01_BROWSER_BASE_URL": management_http_server,
        "P01_BROWSER_AUTHORIZATION": f"Bearer {_FIXTURE_BEARER}",
        "P01_BROWSER_EVIDENCE": str(evidence_path),
    }
    result = subprocess.run(
        ["node", str(_COMPANION)],
        cwd=_BROWSER_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["status"] == "TOOLINGPASS"
    assert evidence["http_status"] == 200
    assert evidence["configured_viewport"] == {"width": 360, "height": 800}
    assert evidence["browser_path_configured"] is True
    assert isinstance(evidence["tooling"]["node"], str)


@pytest.mark.asyncio
async def test_browser_household_onboarding_uses_real_http_ha_database_and_ephemeral_auth(
    tmp_path: Path,
) -> None:
    """Product HTTP journey plus explicitly fault-injected browser presentation cases."""
    from tests.fixtures.household_onboarding_browser import browser_household

    assert os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    async with browser_household(tmp_path) as household:
        evidence = await household.run_browser(tmp_path / "browser-evidence.json")
        assert evidence["status"] == "PASS"
        assert evidence["configured_viewport"] == {"width": 360, "height": 800}
        assert evidence["overflow"] is False and evidence["logged_out"] is True
        assert evidence["evidence"]["value"] == 21.5
        assert evidence["evidence"]["unit"] == "°C"
        assert household.source.reads > 0 and household.source.commands == 0
        assert set(evidence["scenarios"]) >= {
            "R03",
            "R04",
            "R05",
            "R06",
            "R07",
            "R08",
            "R09",
            "R12",
        }
        profiles = await household.gateway.runtime.connection_manager.list()
        assert len(profiles) == 1
        assert str(profiles[0].id) == evidence["evidence"]["profile_id"]


@pytest.mark.asyncio
async def test_manual_browser_reconciles_lost_create_and_duplicate_by_stable_identity(
    tmp_path: Path,
) -> None:
    from tests.fixtures.household_onboarding_browser import browser_household

    async with browser_household(tmp_path) as household:
        evidence = await household.run_browser(tmp_path / "manual-evidence.json", mode="manual")
        assert evidence["status"] == "PASS"
        profiles = await household.gateway.runtime.connection_manager.list()
        assert len(profiles) == 1
        assert str(profiles[0].id) == evidence["profile_id"]
        assert profiles[0].unique_id == evidence["identity"]
        assert profiles[0].enabled is False
        assert household.source.commands == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["validation-edit", "mapping", "manual-form"])
async def test_browser_form_revisions_and_generic_draft_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    from tests.fixtures.household_onboarding_browser import browser_household
    from tests.integration.test_knx_onboarding import _mapping_path

    from domoai.adapters.knx.transport import InMemoryKnxTransport
    from domoai.domain.connections import ConnectionCandidate

    mapping = _mapping_path(tmp_path)
    monkeypatch.setenv("P01_BROWSER_MAPPING", str(mapping))
    transport = InMemoryKnxTransport()
    async with browser_household(tmp_path) as household:
        provider = household.gateway.runtime.connection_manager.providers.get("knx")
        assert provider is not None
        provider.transport_factory = lambda *_: transport
        provider.gateway_discoverer = lambda: [
            ConnectionCandidate(
                connector_id="knx",
                candidate_id="knx:browser:discovery",
                display_name="KNX de ensayo",
                endpoint={"host": "127.0.0.1", "port": 3671},
            )
        ]
        evidence = await household.run_browser(tmp_path / "form-evidence.json", mode=mode)
        assert evidence == {"status": "PASS", "regression": mode}
        profiles = await household.gateway.runtime.connection_manager.list()
        if mode == "mapping":
            assert len(profiles) == 2
            assert all(profile.mapping_ref == str(mapping) for profile in profiles)
            assert all("mapping_ref" not in profile.options for profile in profiles)
            assert transport.connected is True
            assert transport.writes == []
        else:
            assert len(profiles) == 1
            assert profiles[0].endpoint["url"] == household.source.url
            assert profiles[0].enabled is False
        assert household.source.commands == 0
