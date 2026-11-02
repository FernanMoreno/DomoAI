from __future__ import annotations

import json
import logging

import httpx
import pytest

from domoai.management.api import create_management_app


class _Manager:
    def __init__(self) -> None:
        self.calls = 0

    async def create(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        raise AssertionError("management handler must not run")


def _nested_json(depth: int) -> str:
    value: object = "value"
    for _ in range(depth):
        value = [value]
    return json.dumps({"nested": value})


async def _post_body(
    manager: _Manager,
    body: bytes,
    *,
    max_request_body_size: int = 128,
) -> httpx.Response:
    app = create_management_app(
        manager,
        max_request_body_size=max_request_body_size,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(
            "/api/v1/connections",
            content=body,
            headers={"content-type": "application/json"},
        )


@pytest.mark.asyncio
async def test_declared_oversized_management_body_is_rejected_before_parsing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = _Manager()
    secret = "oversized-management-secret"
    caplog.set_level(logging.WARNING, logger="domoai.management.api")

    response = await _post_body(
        manager,
        json.dumps({"credential": secret, "padding": "x" * 256}).encode(),
    )

    assert response.status_code == 413
    assert response.json() == {
        "error": "payload_too_large",
        "message": "request body exceeds configured limit",
    }
    assert secret not in response.text
    assert secret not in caplog.text
    assert "payload_too_large" in caplog.text
    assert manager.calls == 0


@pytest.mark.asyncio
async def test_streamed_oversized_management_body_stops_at_the_limit() -> None:
    manager = _Manager()
    app = create_management_app(manager, max_request_body_size=128)

    async def chunks():
        yield b"{" + b'"padding":"' + b"x" * 256
        raise AssertionError("body reader consumed after the over-limit chunk")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/connections",
            content=chunks(),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json()["error"] == "payload_too_large"
    assert manager.calls == 0


@pytest.mark.parametrize(
    ("body", "expected_error"),
    [
        (_nested_json(33), "payload_too_complex"),
        (json.dumps({f"key-{index}": index for index in range(1001)}), "payload_too_complex"),
        (json.dumps({"text": "x" * 65_537}), "payload_too_complex"),
    ],
)
@pytest.mark.asyncio
async def test_management_json_shape_budgets_reject_without_handler(
    body: str, expected_error: str
) -> None:
    manager = _Manager()
    response = await _post_body(
        manager,
        body.encode(),
        max_request_body_size=2 * 1024 * 1024,
    )

    assert response.status_code == 413
    assert response.json()["error"] == expected_error
    assert response.text not in body
    assert manager.calls == 0


@pytest.mark.asyncio
async def test_invalid_management_json_remains_a_sanitized_bad_request() -> None:
    manager = _Manager()

    response = await _post_body(manager, b"not-json", max_request_body_size=128)

    assert response.status_code == 400
    assert response.json() == {
        "error": "invalid_request",
        "message": "request is invalid",
    }
    assert manager.calls == 0
