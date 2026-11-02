from __future__ import annotations

from types import SimpleNamespace

import pytest

from domoai.config.settings import Settings
from domoai.mcp.gateway import GatewayApplication


class _App:
    def __init__(self) -> None:
        self.mounts: list[tuple[str, object]] = []
        self.router = SimpleNamespace(lifespan_context=None)

    def mount(self, path: str, app: object) -> None:
        self.mounts.append((path, app))

    def add_middleware(self, *args: object, **kwargs: object) -> None:
        return None


class _Server:
    def __init__(self) -> None:
        self.app = _App()

    def streamable_http_app(self) -> _App:
        return self.app


class _Runtime:
    def __init__(self) -> None:
        self.settings = Settings(mcp_max_request_body_size=1234)
        self.connection_manager = object()


@pytest.mark.composition
def test_gateway_propagates_mcp_body_limit_to_management(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_management_app(manager: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        "domoai.management.api.create_management_app",
        fake_create_management_app,
    )

    GatewayApplication(runtime=_Runtime(), server=_Server())

    assert captured["max_request_body_size"] == 1234
