"""Wire-protocol certification: the real ``domoai-mcp`` entry point is reachable
by any standard-protocol MCP client, not just this codebase's own in-process
FastMCP objects."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def _domoai_mcp_command() -> str:
    command = shutil.which("domoai-mcp")
    assert command is not None, "the domoai-mcp console script must be installed (uv sync)"
    return command


@pytest.mark.asyncio
async def test_real_agent_can_connect_discover_and_validate_over_the_wire(
    tmp_path: Path,
) -> None:
    server_params = StdioServerParameters(
        command=_domoai_mcp_command(),
        args=[],
        env={"DOMOAI_DATABASE_PATH": str(tmp_path / "mcp-cert.sqlite3")},
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            try:
                init_result = await session.initialize()
            except Exception as exc:  # pragma: no cover - failure-path message only
                raise AssertionError(f"handshake stage failure: {exc}") from exc
            assert init_result.capabilities is not None, "handshake stage failure: no capabilities"

            try:
                tools = await session.list_tools()
            except Exception as exc:  # pragma: no cover - failure-path message only
                raise AssertionError(f"tool-listing stage failure: {exc}") from exc
            tool_names = {tool.name for tool in tools.tools}
            assert {"discover_devices", "validate_command"} <= tool_names, (
                f"tool-listing stage failure: missing core tools, got {tool_names}"
            )

            try:
                discovery_result = await session.call_tool(
                    "discover_devices", {"refresh": False}
                )
            except Exception as exc:  # pragma: no cover - failure-path message only
                raise AssertionError(f"tool-call stage failure (discover_devices): {exc}") from exc
            assert discovery_result.isError is not True, (
                f"tool-call stage failure: discover_devices returned an error: {discovery_result}"
            )
            discovery: dict[str, Any] = discovery_result.structuredContent or {}
            devices = discovery.get("devices", [])
            assert isinstance(devices, list) and len(devices) > 0, (
                "tool-call stage failure: discover_devices returned no devices"
            )

            device_id = devices[0]["id"]
            command = {
                "id": "mcp-protocol-cert-1",
                "device_id": device_id,
                "command": "turn_on",
                "idempotency_key": "mcp-protocol-cert-intent-1",
            }
            try:
                validation_result = await session.call_tool(
                    "validate_command", {"command": command}
                )
            except Exception as exc:  # pragma: no cover - failure-path message only
                raise AssertionError(f"tool-call stage failure (validate_command): {exc}") from exc
            assert validation_result.isError is not True, (
                f"tool-call stage failure: validate_command returned an error: {validation_result}"
            )
            validation: dict[str, Any] = validation_result.structuredContent or {}
            assert validation["command"]["device_id"] == device_id
            assert validation["validation"] is not None, (
                "postcondition stage failure: validate_command produced no validation result"
            )
