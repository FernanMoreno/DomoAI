from __future__ import annotations

import httpx
import pytest

from domoai.management.api import create_management_app


class _Manager:
    calls = 0

    async def create(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        raise AssertionError("bounded request must not reach the manager")


@pytest.mark.asyncio
async def test_management_payload_limit_contract_is_sanitized() -> None:
    manager = _Manager()
    app = create_management_app(manager, max_request_body_size=32)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/connections",
            content=b'{"credential":"contract-secret","padding":"xxxxxxxx"}',
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "error": "payload_too_large",
        "message": "request body exceeds configured limit",
    }
    assert "contract-secret" not in response.text
    assert manager.calls == 0
