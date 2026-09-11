"""Public shell and CSP transport contract; browser tests own UI behavior."""

from __future__ import annotations

import base64
import hashlib
import re
from html.parser import HTMLParser

import httpx
import pytest

from domoai.management.api import create_management_app


class _Shell(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


@pytest.mark.asyncio
async def test_public_spanish_shell_is_identical_before_auth_and_does_not_cache() -> None:
    app = create_management_app(object(), auth_token="fixture-admin")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/")
        authorized = await client.get("/", headers={"Authorization": "Bearer fixture-admin"})
        head = await client.head("/")
        private = await client.get("/api/v1/connections")
    assert response.status_code == authorized.status_code == head.status_code == 200
    assert response.content == authorized.content
    assert not head.content
    assert private.status_code == 401
    assert "no-store" in response.headers["cache-control"]
    assert "fixture-admin" not in response.text
    shell = _Shell()
    shell.feed(response.text)
    assert ("html", {"lang": "es-ES"}) in shell.elements
    inputs = [attrs for tag, attrs in shell.elements if tag == "input"]
    management = next(attrs for attrs in inputs if attrs.get("id") == "management-token")
    assert management["type"] == "password" and management["autocomplete"] == "off"
    assert not management.get("value")
    assert any(attrs.get("aria-live") == "polite" for _, attrs in shell.elements)
    assert not any(key.startswith("on") for _, attrs in shell.elements for key in attrs)


@pytest.mark.asyncio
async def test_csp_authorizes_exact_shell_scripts_without_unsafe_inline_javascript() -> None:
    app = create_management_app(object())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/")
    csp = response.headers["content-security-policy"]
    script_policy = next(part for part in csp.split(";") if part.strip().startswith("script-src"))
    assert "'unsafe-inline'" not in script_policy
    scripts = re.findall(rb"<script[^>]*>(.*?)</script>", response.content, re.S)
    assert scripts
    for script in scripts:
        digest = base64.b64encode(hashlib.sha256(script).digest()).decode()
        assert f"'sha256-{digest}'" in script_policy
