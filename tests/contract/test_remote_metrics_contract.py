from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from domoai.config.settings import Settings
from domoai.mcp.gateway import create_gateway_server
from tests.unit.mcp.test_gateway import _build_context


class _Metrics:
    def __init__(self, snapshot: dict[str, object]) -> None:
        self.snapshot_data = snapshot
        self.calls = 0

    async def snapshot(self) -> dict[str, object]:
        self.calls += 1
        return self.snapshot_data


def _write_token_file(path: Path, token: str = "metrics-secret") -> None:
    path.write_text(
        json.dumps(
            {
                "clients": [
                    {
                        "client_id": "monitoring",
                        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                        "scopes": ["read"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _snapshot() -> dict[str, object]:
    return {
        "event_consumer_alive": True,
        "scheduler_alive": True,
        "operational": {
            "command_outcomes": {"confirmed_success": 1},
            "command_latency_ms": [],
            "source_cursor": {},
            "leases": {},
            "approvals": {},
            "bundles": {},
        },
    }


async def _get_metrics(
    settings: Settings,
    metrics: _Metrics,
    tmp_path: Path,
    headers: dict[str, str] | None = None,
) -> tuple[httpx.Response, _Metrics]:
    context = await _build_context()
    context.domotics.metrics = metrics
    server = create_gateway_server(context, settings, runtime=object())
    transport = httpx.ASGITransport(app=server.streamable_http_app())
    async with httpx.AsyncClient(
        transport=transport, base_url=settings.mcp_public_url
    ) as client:
        response = await client.get("/metrics", headers=headers)
    return response, metrics


@pytest.mark.asyncio
async def test_metrics_route_is_disabled_by_default(tmp_path: Path) -> None:
    metrics = _Metrics(_snapshot())
    response, metrics = await _get_metrics(
        Settings(database_path=tmp_path / "metrics.sqlite3"), metrics, tmp_path
    )

    assert response.status_code == 404
    assert metrics.calls == 0


@pytest.mark.asyncio
async def test_metrics_route_requires_bearer_without_reading_snapshot(tmp_path: Path) -> None:
    token_file = tmp_path / "clients.json"
    _write_token_file(token_file)
    settings = Settings(
        database_path=tmp_path / "metrics.sqlite3",
        mcp_client_token_file=token_file,
        mcp_metrics_enabled=True,
    )
    metrics = _Metrics(_snapshot())

    response, metrics = await _get_metrics(settings, metrics, tmp_path)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert "confirmed_success" not in response.text
    assert metrics.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authorization",
    ["Bearer invalid", "Basic metrics-secret", "Bearer metrics-secret extra"],
)
async def test_metrics_route_rejects_invalid_bearer_without_snapshot(
    tmp_path: Path, authorization: str
) -> None:
    token_file = tmp_path / "clients.json"
    _write_token_file(token_file)
    settings = Settings(
        database_path=tmp_path / "metrics.sqlite3",
        mcp_client_token_file=token_file,
        mcp_metrics_enabled=True,
    )
    metrics = _Metrics(_snapshot())

    response, metrics = await _get_metrics(
        settings,
        metrics,
        tmp_path,
        headers={"Authorization": authorization},
    )

    assert response.status_code == 401
    assert metrics.calls == 0


@pytest.mark.asyncio
async def test_metrics_route_returns_bounded_prometheus_snapshot_for_valid_bearer(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "clients.json"
    _write_token_file(token_file)
    settings = Settings(
        database_path=tmp_path / "metrics.sqlite3",
        mcp_client_token_file=token_file,
        mcp_metrics_enabled=True,
    )
    metrics = _Metrics(_snapshot())

    response, metrics = await _get_metrics(
        settings,
        metrics,
        tmp_path,
        headers={"Authorization": "Bearer metrics-secret"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert "domoai_up 1" in response.text
    assert "metrics-secret" not in response.text
    assert metrics.calls == 1


@pytest.mark.asyncio
async def test_metrics_route_fails_closed_on_oversized_snapshot(tmp_path: Path) -> None:
    token_file = tmp_path / "clients.json"
    _write_token_file(token_file)
    settings = Settings(
        database_path=tmp_path / "metrics.sqlite3",
        mcp_client_token_file=token_file,
        mcp_metrics_enabled=True,
        mcp_metrics_max_bytes=32,
    )
    metrics = _Metrics(_snapshot())

    response, _ = await _get_metrics(
        settings,
        metrics,
        tmp_path,
        headers={"Authorization": "Bearer metrics-secret"},
    )

    assert response.status_code == 503
    assert "domoai_up" not in response.text
