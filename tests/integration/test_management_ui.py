from __future__ import annotations

import httpx
import pytest

from domoai.management.api import create_management_app


@pytest.mark.asyncio
async def test_first_install_ui_exposes_all_p0_operator_states() -> None:
    app = create_management_app(object())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/")

    assert response.status_code == 200
    for state in (
        "first-install",
        "found systems",
        "manual add",
        "password",
        "mapping",
        "success",
        "existing",
        "connected",
        "degraded",
        "reauth",
        "diagnostics",
        "remove",
        "advanced settings",
    ):
        assert state in response.text.casefold()
    assert "/api/v1/connection-providers" in response.text
    assert "renderProviderCatalog" in response.text
    assert "setup.fields" in response.text
