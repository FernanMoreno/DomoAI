"""Vendor-neutral, read-only MCP compatibility probe."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, cast
from urllib.parse import urlparse

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import AnyUrl

RUNTIME_RESOURCE_URI = "domotics://runtime"
DEFAULT_REQUIRED_TOOLS = frozenset(
    {
        "discover_devices",
        "get_state",
        "validate_plan",
        "execute_plan",
        "optimize_scenario",
    }
)


class ProbeFailure(Exception):
    """A stable, deliberately secret-free probe failure."""

    def __init__(self, category: str) -> None:
        allowed_categories = {
            "configuration",
            "transport",
            "authentication",
            "catalog",
            "resource",
            "discovery",
        }
        if category not in allowed_categories:
            raise ValueError(f"unsupported probe failure category: {category}")
        self.category = category
        super().__init__(category)


@dataclass(frozen=True, slots=True)
class ClientSessionEvidence:
    """Canonical probe output; intentionally contains no credentials."""

    status: str
    client_label: str
    endpoint: str
    catalog_digest: str
    runtime_revision: str
    registry_digest: str
    discovery_digest: str

    def as_dict(self) -> dict[str, str]:
        return cast(dict[str, str], asdict(self))


def canonical_json(value: object) -> str:
    """Serialize JSON-compatible data deterministically for evidence hashes."""

    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as error:
        raise ProbeFailure("resource") from error


def digest_json(value: object) -> str:
    """Return a stable, non-secret SHA-256 digest for structured evidence."""

    payload = canonical_json(value).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def redacted_error(error: ProbeFailure) -> dict[str, str]:
    """Convert a failure to the only error shape the CLI exposes."""

    return {"status": "error", "category": error.category}


def token_from_environment_or_stdin() -> tuple[str, str]:
    """Read a bearer from a secret channel without accepting command arguments."""

    token = os.environ.get("DOMOAI_MCP_PROBE_TOKEN", "").strip()
    if token:
        return token, "environment"
    if sys.stdin.isatty():
        raise ProbeFailure("configuration")
    token = sys.stdin.readline().strip()
    if not token:
        raise ProbeFailure("configuration")
    return token, "stdin"


def _validate_endpoint(endpoint: str) -> None:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ProbeFailure("configuration")
    if parsed.username is not None or parsed.password is not None:
        raise ProbeFailure("configuration")


def _category_for_exception(error: Exception) -> str:
    if isinstance(error, ProbeFailure):
        return error.category
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in {401, 403}:
        return "authentication"
    if isinstance(error, httpx.HTTPError):
        return "transport"
    return "transport"


def _resource_json(result: Any) -> Mapping[str, Any]:
    contents = getattr(result, "contents", None)
    if not isinstance(contents, list):
        raise ProbeFailure("resource")
    for content in contents:
        text = getattr(content, "text", None)
        if not isinstance(text, str):
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            raise ProbeFailure("resource") from error
        if isinstance(value, Mapping):
            return value
        raise ProbeFailure("resource")
    raise ProbeFailure("resource")


def _discovery_json(result: Any) -> Mapping[str, Any]:
    value = getattr(result, "structuredContent", None)
    if isinstance(value, Mapping):
        return value
    raise ProbeFailure("discovery")


@dataclass(frozen=True, slots=True)
class MCPClientProbe:
    """Run the fixed read-only compatibility handshake over Streamable HTTP."""

    endpoint: str
    token: str
    client_label: str = "generic"
    required_tools: frozenset[str] = DEFAULT_REQUIRED_TOOLS

    async def run(self) -> ClientSessionEvidence:
        _validate_endpoint(self.endpoint)
        if not self.token:
            raise ProbeFailure("configuration")
        if not self.client_label or not self.client_label.strip():
            raise ProbeFailure("configuration")

        seen_status_codes: list[int] = []

        async def record_response(response: httpx.Response) -> None:
            seen_status_codes.append(response.status_code)

        try:
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {self.token}"},
                event_hooks={"response": [record_response]},
            ) as http_client:
                async with streamable_http_client(
                    self.endpoint, http_client=http_client
                ) as (read_stream, write_stream, _):
                    try:
                        async with ClientSession(read_stream, write_stream) as session:
                            await session.initialize()
                            tools_result = await session.list_tools()
                            tool_names = {tool.name for tool in tools_result.tools}
                            missing = self.required_tools - tool_names
                            if missing:
                                raise ProbeFailure("catalog")

                            runtime_result = await session.read_resource(
                                cast(AnyUrl, RUNTIME_RESOURCE_URI)
                            )
                            runtime = _resource_json(runtime_result)
                            runtime_revision = runtime.get("runtime_revision")
                            if not isinstance(runtime_revision, str) or not runtime_revision:
                                raise ProbeFailure("resource")

                            discovery_result = await session.call_tool(
                                "discover_devices", {"refresh": False}
                            )
                            discovery = _discovery_json(discovery_result)
                            if discovery.get("runtime_revision") != runtime_revision:
                                raise ProbeFailure("discovery")
                            devices = discovery.get("devices")
                            areas = discovery.get("areas")
                            if not isinstance(devices, list) or not isinstance(areas, list):
                                raise ProbeFailure("discovery")

                            # The SDK delivers the response before its POST
                            # task has necessarily left httpx's response
                            # context. Yield once so an immediate probe exit
                            # cannot cancel that close and leave a socket for
                            # garbage collection.
                            await asyncio.sleep(0)

                            return ClientSessionEvidence(
                                status="ok",
                                client_label=self.client_label.strip(),
                                endpoint=self.endpoint,
                                catalog_digest=digest_json(sorted(tool_names)),
                                runtime_revision=runtime_revision,
                                registry_digest=digest_json(
                                    {"devices": devices, "areas": areas}
                                ),
                                discovery_digest=digest_json(discovery),
                            )
                    finally:
                        # The MCP SDK closes these streams through its task
                        # groups, but explicit closure makes ownership
                        # deterministic under asyncio debug and during a
                        # concurrent server shutdown.
                        await read_stream.aclose()
                        await write_stream.aclose()
        except ProbeFailure:
            raise
        except Exception as error:  # noqa: BLE001 - map all transport details to a stable code
            if any(status in {401, 403} for status in seen_status_codes):
                raise ProbeFailure("authentication") from error
            raise ProbeFailure(_category_for_exception(error)) from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a DomoAI MCP endpoint using read-only operations."
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("DOMOAI_MCP_PROBE_URL", ""),
        help="Streamable HTTP MCP endpoint (or DOMOAI_MCP_PROBE_URL)",
    )
    parser.add_argument("--client-label", default="generic")
    return parser


async def _run_cli(arguments: argparse.Namespace) -> int:
    try:
        if not arguments.url:
            raise ProbeFailure("configuration")
        token, _ = token_from_environment_or_stdin()
        evidence = await MCPClientProbe(
            endpoint=arguments.url,
            token=token,
            client_label=arguments.client_label,
        ).run()
    except ProbeFailure as error:
        json.dump(redacted_error(error), sys.stderr, sort_keys=True, separators=(",", ":"))
        sys.stderr.write("\n")
        return 1
    except Exception:  # noqa: BLE001 - CLI must never expose provider/transport details
        json.dump({"status": "error", "category": "transport"}, sys.stderr)
        sys.stderr.write("\n")
        return 1

    json.dump(evidence.as_dict(), sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run_cli(_parser().parse_args())))
