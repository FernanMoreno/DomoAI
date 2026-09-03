import json
from types import SimpleNamespace
from typing import Any

import pytest

from domoai.mcp.probe import (
    DEFAULT_REQUIRED_TOOLS,
    ClientSessionEvidence,
    MCPClientProbe,
    ProbeFailure,
    canonical_json,
    digest_json,
    redacted_error,
    token_from_environment_or_stdin,
)


def test_canonical_json_and_digest_are_stable() -> None:
    assert canonical_json({"b": 2, "a": [True, "x"]}) == '{"a":[true,"x"],"b":2}'
    assert digest_json({"b": 2, "a": [True, "x"]}) == digest_json(
        {"a": [True, "x"], "b": 2}
    )
    assert digest_json({"a": 1}).startswith("sha256:")


def test_evidence_is_secret_free() -> None:
    evidence = ClientSessionEvidence(
        status="ok",
        client_label="codex",
        endpoint="http://127.0.0.1:8124/mcp",
        catalog_digest="sha256:catalog",
        runtime_revision="runtime-1",
        registry_digest="sha256:registry",
        discovery_digest="sha256:discovery",
    )

    serialized = json.dumps(evidence.as_dict(), sort_keys=True)
    assert "Bearer" not in serialized
    assert "super-secret" not in serialized
    assert evidence.as_dict()["status"] == "ok"


def test_token_source_never_accepts_a_command_line_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMOAI_MCP_PROBE_TOKEN", "super-secret")
    assert token_from_environment_or_stdin() == ("super-secret", "environment")


def test_token_source_reads_stdin_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOMOAI_MCP_PROBE_TOKEN", raising=False)
    monkeypatch.setattr(
        "sys.stdin",
        SimpleNamespace(isatty=lambda: False, readline=lambda: "stdin-secret\n"),
    )
    assert token_from_environment_or_stdin() == ("stdin-secret", "stdin")


def test_probe_errors_are_stable_and_redacted() -> None:
    error = ProbeFailure("authentication")
    payload = redacted_error(error)
    assert payload == {"status": "error", "category": "authentication"}
    assert "secret" not in json.dumps(payload).lower()


@pytest.mark.asyncio
async def test_probe_requires_the_common_read_only_catalog() -> None:
    probe = MCPClientProbe(
        endpoint="http://127.0.0.1:8124/mcp",
        token="super-secret",
        client_label="codex",
    )

    assert probe.required_tools == DEFAULT_REQUIRED_TOOLS


@pytest.mark.asyncio
async def test_probe_result_contains_canonical_runtime_and_discovery_digests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStream:
        async def aclose(self) -> None:
            return None

    class FakeContext:
        async def __aenter__(self) -> tuple[Any, Any, Any]:
            return FakeStream(), FakeStream(), lambda: "session-id"

        async def __aexit__(self, *_: object) -> None:
            return None

    class FakeSession:
        def __init__(self, *_: object) -> None:
            pass

        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def initialize(self) -> None:
            return None

        async def list_tools(self) -> Any:
            return SimpleNamespace(
                tools=[SimpleNamespace(name=name) for name in DEFAULT_REQUIRED_TOOLS]
            )

        async def read_resource(self, _: object) -> Any:
            return SimpleNamespace(
                contents=[
                    SimpleNamespace(
                        text=json.dumps(
                            {
                                "schema_version": "v1",
                                "runtime_revision": "runtime-1",
                                "writable_capabilities": [],
                            }
                        )
                    )
                ]
            )

        async def call_tool(self, name: str, arguments: dict[str, object]) -> Any:
            assert name == "discover_devices"
            assert arguments == {"refresh": False}
            return SimpleNamespace(
                structuredContent={
                    "schema_version": "v1",
                    "runtime_revision": "runtime-1",
                    "devices": [],
                    "areas": [],
                }
            )

    monkeypatch.setattr(
        "domoai.mcp.probe.streamable_http_client",
        lambda *args, **kwargs: FakeContext(),
    )
    monkeypatch.setattr("domoai.mcp.probe.ClientSession", FakeSession)

    evidence = await MCPClientProbe(
        endpoint="http://127.0.0.1:8124/mcp",
        token="super-secret",
        client_label="codex",
    ).run()

    assert evidence.runtime_revision == "runtime-1"
    assert evidence.catalog_digest.startswith("sha256:")
    assert evidence.registry_digest.startswith("sha256:")
    assert evidence.discovery_digest.startswith("sha256:")
