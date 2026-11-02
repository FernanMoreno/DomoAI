from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from domoai.adapters.knx.ets import EtsImporter
from domoai.management.api import create_management_app


class _Manager:
    async def list(self) -> list[object]:
        return []


@pytest.mark.asyncio
async def test_ets_management_routes_return_preview_and_gate_persistence(tmp_path: Path) -> None:
    source = tmp_path / "project.xml"
    source.write_text(
        "<Project name='House'><GroupAddress address='1/0/1' dpt='1.001' name='Light'/></Project>",
        encoding="utf-8",
    )
    target = tmp_path / "mapping.json"
    app = create_management_app(_Manager(), ets_importer=EtsImporter())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        preview_response = await client.post(
            "/api/v1/knx/ets/preview", json={"source_path": str(source)}
        )
        assert preview_response.status_code == 200
        preview = preview_response.json()
        rejected = await client.post(
            "/api/v1/knx/ets/persist",
            json={
                "preview": preview,
                "source_path": str(source),
                "target_path": str(target),
                "confirmed": False,
            },
        )

    assert rejected.status_code == 409
    assert not target.exists()
