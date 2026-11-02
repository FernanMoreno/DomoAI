"""Disposable HTTP household kept alive across browser and independent SDK use."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import patch

import uvicorn

from domoai.domain.connections import ConnectionCandidate
from domoai.management.api import create_management_app
from tests.fixtures.household_onboarding import home_assistant_http_source, onboarding_gateway

_BROWSER_DIR = Path(__file__).parents[1] / "browser"


@dataclass
class BrowserHousehold:
    gateway: Any
    source: Any
    base_url: str
    management_token: str
    read_token: str

    async def run_browser(self, evidence_path: Path, *, mode: str = "product") -> dict:
        process = await asyncio.create_subprocess_exec(
            "node",
            str(_BROWSER_DIR / "run_household_onboarding_browser.cjs"),
            cwd=_BROWSER_DIR,
            env={
                **os.environ,
                "P01_BROWSER_BASE_URL": self.base_url,
                "P01_BROWSER_MODE": mode,
                "P01_BROWSER_TOKEN": self.management_token,
                "P01_BROWSER_READ_TOKEN": self.read_token,
                "P01_BROWSER_HA_URL": self.source.url,
                "P01_BROWSER_EVIDENCE": str(evidence_path),
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        assert process.returncode == 0, stderr.decode()
        evidence = json.loads(evidence_path.read_text())
        assert self.management_token not in stdout.decode() + stderr.decode()
        assert self.read_token not in stdout.decode() + stderr.decode()
        return evidence


@asynccontextmanager
async def browser_household(tmp_path: Path):
    management_token = "p01-browser-temporary-management-token"
    read_token = "p01-browser-separate-read-token"
    clients = []
    for name, token, scopes in (
        ("browser-operator", management_token, ["read", "manage"]),
        ("external-reader", read_token, ["read"]),
    ):
        clients.append(
            {
                "client_id": name,
                "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                "scopes": scopes,
                "tenant_id": "tenant-a",
                "household_ids": ["home-a"],
                "roles": ["service"],
            }
        )
    token_path = tmp_path / "browser-clients.json"
    token_path.write_text(json.dumps({"clients": clients}))
    # This fixture batches many independent UI faults in seconds. Only its request
    # budget is enlarged; the real origin, bearer, authority and limiter remain active.
    with (
        socket.socket() as listener,
        patch(
            "domoai.management.api.create_management_app",
            partial(create_management_app, rate_limit=1000),
        ),
    ):
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        async with (
            home_assistant_http_source() as source,
            onboarding_gateway(
                tmp_path,
                mcp_client_token_file=token_path,
                mcp_public_url=base_url,
                mcp_port=port,
            ) as gateway,
        ):
            provider = gateway.runtime.connection_manager.providers.get("home_assistant")
            assert provider is not None
            provider.mdns_discover = lambda: [
                ConnectionCandidate(
                    connector_id="home_assistant",
                    candidate_id="ha:salon",
                    display_name="Salón",
                    endpoint={"url": source.url},
                    metadata={"discovery": "fixture"},
                )
            ]
            server = uvicorn.Server(
                uvicorn.Config(gateway.app, log_level="critical", lifespan="on")
            )
            task = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                for _ in range(100):
                    if server.started:
                        break
                    if task.done():
                        await task
                    await asyncio.sleep(0.02)
                assert server.started
                yield BrowserHousehold(gateway, source, base_url, management_token, read_token)
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, timeout=10)
