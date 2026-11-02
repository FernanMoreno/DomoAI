from datetime import UTC, datetime

import pytest

from domoai.mcp.qualification import (
    ClientQualificationEvidence,
    QualificationStage,
    qualification_configuration_digest,
)


def _stages() -> tuple[QualificationStage, ...]:
    return (
        QualificationStage("connect", "passed", "sdk-connect"),
        QualificationStage("initialize", "passed", "sdk-initialize"),
        QualificationStage("catalog", "passed", "sdk-catalog"),
        QualificationStage("runtime_read", "passed", "sdk-runtime-read"),
        QualificationStage("scope_denial", "passed", "sdk-scope", "insufficient_scope"),
        QualificationStage("reconnect", "passed", "sdk-reconnect"),
        QualificationStage("clean_shutdown", "passed", "sdk-cleanup"),
    )


def _evidence(**changes: object) -> ClientQualificationEvidence:
    values: dict[str, object] = {
        "qualification_id": "run-1",
        "client_name": "generic-sdk-local",
        "client_version": "1.29.0",
        "client_build": None,
        "system": {"os": "Linux", "os_version": "6.1", "python": "3.12.0"},
        "transport": "streamable-http",
        "auth_mode": "incoming-static-bearer-a05",
        "negotiated_protocol_version": "2025-06-18",
        "server_name": "DomoAI",
        "server_version": "1.0.0",
        "server_revision": "build-1",
        "client_source_snapshot_digest": "sha256:" + "a" * 64,
        "server_source_snapshot_digest": "sha256:" + "b" * 64,
        "client_source_snapshot_scope": (
            "src/domoai/mcp/qualification.py",
            "tests/integration/test_mcp_gateway_http.py",
        ),
        "server_source_snapshot_scope": ("src/domoai/mcp/resources.py",),
        "configuration_digest": "sha256:" + "c" * 64,
        "tested_at": datetime.now(UTC).isoformat(),
        "runtime_revision": "runtime-1",
        "scenarios": _stages(),
        "status": "minimum-qualified",
        "limitations": ("local-sdk-only",),
    }
    values.update(changes)
    return ClientQualificationEvidence(**values)  # type: ignore[arg-type]


def test_qualification_evidence_rejects_unbounded_digest_or_system_provenance() -> None:
    with pytest.raises(ValueError):
        _evidence(configuration_digest="sha256:not-a-digest")
    with pytest.raises(ValueError):
        _evidence(system={"os": "Linux", "python": "3.12.0"})


def test_qualification_evidence_rejects_untrusted_runtime_values() -> None:
    with pytest.raises(ValueError):
        QualificationStage("connect", "invented", "sdk-connect")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        _evidence(status="full-qualified")
    with pytest.raises(ValueError):
        _evidence(server_revision=None, scenarios=tuple(
            QualificationStage(stage.name, "failed", stage.evidence_ref)
            for stage in _stages()
        ))
    with pytest.raises(ValueError):
        _evidence(client_source_snapshot_scope=("../private.py",))
    with pytest.raises(ValueError):
        _evidence(server_source_snapshot_scope=("/home/private.py",))
    with pytest.raises(ValueError):
        _evidence(limitations=("Bearer leaked-value",))
    with pytest.raises(ValueError):
        _evidence(schema_version="domoai.client-qualification.v2")
    with pytest.raises(ValueError):
        _evidence(transport="stdio")
    with pytest.raises(ValueError):
        _evidence(auth_mode="Bearer-secret")


def test_qualification_configuration_digest_accepts_only_public_typed_allowlist() -> None:
    configuration = {
        "auth_mode": "incoming-static-bearer-a05",
        "json_response": True,
        "server_sent_events": False,
        "transport": "streamable-http",
    }

    assert qualification_configuration_digest(configuration).startswith("sha256:")

    for invalid in (
        {**configuration, "auth_mode": "Bearer leaked-value"},
        {**configuration, "json_response": "Bearer leaked-value"},
        {**configuration, "server_sent_events": 1},
        {key: value for key, value in configuration.items() if key != "transport"},
    ):
        with pytest.raises(ValueError):
            qualification_configuration_digest(invalid)
