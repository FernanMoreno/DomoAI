"""Bounded, non-authoritative evidence types for local MCP qualification."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from domoai.mcp.probe import digest_json

_SAFE_EVIDENCE_VALUE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SAFE_SOURCE_PATH = re.compile(r"^[A-Za-z0-9._/-]{1,256}$")
_SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SCHEMA_VERSION = "domoai.client-qualification.v1"
_STAGE_STATUSES = frozenset({"passed", "failed", "blocked", "cancelled"})
_QUALIFICATION_STATUSES = frozenset({"minimum-qualified", "failed", "blocked", "cancelled"})
_LIMITATION_CODES = frozenset(
    {"local-sdk-only", "no-named-agent-ui", "source-snapshot-changed"}
)

StageName = Literal[
    "connect",
    "initialize",
    "catalog",
    "runtime_read",
    "scope_denial",
    "reconnect",
    "clean_shutdown",
]
StageStatus = Literal["passed", "failed", "blocked", "cancelled"]
QualificationStatus = Literal["minimum-qualified", "failed", "blocked", "cancelled"]
_REQUIRED_STAGES = frozenset(
    {
        "connect",
        "initialize",
        "catalog",
        "runtime_read",
        "scope_denial",
        "reconnect",
        "clean_shutdown",
    }
)


@dataclass(frozen=True, slots=True)
class QualificationStage:
    """A result produced by one actual local qualification stage."""

    name: StageName
    status: StageStatus
    evidence_ref: str
    expected_error_code: str | None = None

    def __post_init__(self) -> None:
        if self.name not in _REQUIRED_STAGES or self.status not in _STAGE_STATUSES:
            raise ValueError("qualification stage has an unsupported name or status")
        if not _SAFE_EVIDENCE_VALUE.fullmatch(self.evidence_ref):
            raise ValueError("qualification evidence reference is not safe")
        if (
            self.expected_error_code is not None
            and not _SAFE_EVIDENCE_VALUE.fullmatch(self.expected_error_code)
        ):
            raise ValueError("qualification error code is not safe evidence")


def source_snapshot_digest(paths: Iterable[Path]) -> str:
    """Fingerprint exact tested sources, including local uncommitted contents."""

    digest = hashlib.sha256()
    for path in sorted((Path(item).resolve() for item in paths), key=lambda item: str(item)):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def qualification_configuration_digest(configuration: Mapping[str, object]) -> str:
    """Hash an explicit public allowlist assembled by the test-owned runner."""

    allowed = {
        key: value
        for key, value in configuration.items()
        if key in {"auth_mode", "json_response", "server_sent_events", "transport"}
    }
    if set(allowed) != {"auth_mode", "json_response", "server_sent_events", "transport"}:
        raise ValueError("qualification configuration allowlist is incomplete")
    if (
        allowed["auth_mode"] != "incoming-static-bearer-a05"
        or allowed["transport"] != "streamable-http"
        or type(allowed["json_response"]) is not bool
        or type(allowed["server_sent_events"]) is not bool
    ):
        raise ValueError("qualification configuration allowlist has invalid values")
    return digest_json(allowed)


def _is_repository_relative_scope(path: str) -> bool:
    candidate = PurePosixPath(path)
    return (
        bool(_SAFE_SOURCE_PATH.fullmatch(path))
        and not candidate.is_absolute()
        and all(part not in {"", ".", ".."} for part in candidate.parts)
    )


@dataclass(frozen=True, slots=True)
class ClientQualificationEvidence:
    """Secret-free evidence; it cannot create authority or durable qualification state."""

    qualification_id: str
    client_name: str
    client_version: str
    client_build: str | None
    system: dict[str, str]
    transport: str
    auth_mode: str
    negotiated_protocol_version: str | None
    server_name: str | None
    server_version: str | None
    server_revision: str | None
    client_source_snapshot_digest: str
    server_source_snapshot_digest: str
    client_source_snapshot_scope: tuple[str, ...]
    server_source_snapshot_scope: tuple[str, ...]
    configuration_digest: str
    tested_at: str
    runtime_revision: str | None
    scenarios: tuple[QualificationStage, ...]
    status: QualificationStatus
    limitations: tuple[str, ...]
    schema_version: str = "domoai.client-qualification.v1"

    def __post_init__(self) -> None:
        if self.schema_version != _SCHEMA_VERSION or self.status not in _QUALIFICATION_STATUSES:
            raise ValueError("qualification evidence has an unsupported schema or status")
        if (
            self.transport != "streamable-http"
            or self.auth_mode != "incoming-static-bearer-a05"
        ):
            raise ValueError("qualification evidence has unsupported transport or authentication")
        safe_fields = (
            self.client_name,
            self.client_version,
            self.client_build,
            self.transport,
            self.auth_mode,
            self.negotiated_protocol_version,
            self.server_name,
            self.server_version,
            self.server_revision,
            self.runtime_revision,
            self.qualification_id,
        )
        if any(
            value is not None and not _SAFE_EVIDENCE_VALUE.fullmatch(value)
            for value in safe_fields
        ):
            raise ValueError("qualification evidence includes an unsafe value")
        if not all(
            _SHA256_DIGEST.fullmatch(value)
            for value in (
                self.client_source_snapshot_digest,
                self.server_source_snapshot_digest,
                self.configuration_digest,
            )
        ):
            raise ValueError("qualification evidence requires SHA-256 digests")
        if not self.client_source_snapshot_scope or not self.server_source_snapshot_scope:
            raise ValueError("qualification evidence requires declared source scopes")
        if any(
            not _is_repository_relative_scope(path)
            for path in self.client_source_snapshot_scope + self.server_source_snapshot_scope
        ):
            raise ValueError("qualification source scope is unsafe")
        if not set(self.limitations) <= _LIMITATION_CODES:
            raise ValueError("qualification limitations are unsupported")
        if set(self.system) != {"os", "os_version", "python"} or any(
            not _SAFE_EVIDENCE_VALUE.fullmatch(value) for value in self.system.values()
        ):
            raise ValueError("qualification system provenance is incomplete or unsafe")
        try:
            observed_at = datetime.fromisoformat(self.tested_at)
        except ValueError as error:
            raise ValueError("qualification timestamp is invalid") from error
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("qualification timestamp must be timezone-aware")
        if set(stage.name for stage in self.scenarios) != _REQUIRED_STAGES:
            raise ValueError("qualification evidence must name every required stage exactly once")
        if len({stage.name for stage in self.scenarios}) != len(self.scenarios):
            raise ValueError("qualification evidence cannot duplicate stages")
        if self.status == "minimum-qualified":
            if self.server_revision is None:
                raise ValueError("qualified evidence requires an explicit server build revision")
            if (
                self.negotiated_protocol_version is None
                or self.server_name is None
                or self.server_version is None
                or self.runtime_revision is None
            ):
                raise ValueError(
                    "qualified evidence requires observed protocol and server identity"
                )
            if any(stage.status != "passed" for stage in self.scenarios):
                raise ValueError("qualified evidence requires every observed stage to pass")
            scope = next(stage for stage in self.scenarios if stage.name == "scope_denial")
            if scope.expected_error_code != "insufficient_scope":
                raise ValueError("qualified evidence requires observed insufficient_scope denial")

    def as_dict(self) -> dict[str, object]:
        """Serialize evidence without endpoint, credential, path or entity data."""

        return cast(dict[str, object], asdict(self))
