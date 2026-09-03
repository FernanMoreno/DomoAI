"""Safe, opt-in bootstrap for known local runtime services."""

from __future__ import annotations

import os
import socket
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field

from domoai.config.settings import Settings
from domoai.domain.models import StrictModel

BootstrapProfile = Literal["none", "lab"]
BootstrapStatus = Literal["configured", "auto_configured", "unavailable", "skipped"]
Probe = Callable[[str, int], bool]


class BootstrapCandidate(StrictModel):
    """Non-secret result for one allowlisted provider candidate."""

    provider_id: str = Field(min_length=1, max_length=64)
    endpoint: str = Field(min_length=1, max_length=256)
    status: BootstrapStatus
    mapping_path: str | None = Field(default=None, max_length=512)
    operational_paths: list[str] = Field(default_factory=list, max_length=8)
    reason_code: str | None = Field(default=None, max_length=128)


class RuntimeBootstrapManifest(StrictModel):
    """Persisted diagnostic record; never an authority or credential store."""

    schema_version: Literal["v1"] = "v1"
    profile: BootstrapProfile
    resolved_at: datetime
    candidates: list[BootstrapCandidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list, max_length=32)


@dataclass(frozen=True)
class RuntimeBootstrapResult:
    settings: Settings
    manifest: RuntimeBootstrapManifest
    manifest_path: Path | None


class RuntimeBootstrap:
    """Resolve only server-known local candidates, with explicit precedence."""

    _LAB_HOST = "127.0.0.1"
    # KNX Virtual listens on its Windows-side upstream port 3671.  The WSL
    # knxd endpoint used by DomoAI is deliberately separate to avoid sharing
    # the tunnel port with the upstream connection.
    _LAB_KNX_GATEWAY_PORT = 3672
    _LAB_CANDIDATES = {
        "home_assistant": ("http://127.0.0.1:8123", 8123),
        "zigbee2mqtt": ("mqtt://127.0.0.1:1883", 1883),
        "matter": ("ws://127.0.0.1:5580/ws", 5580),
        "modbus": ("tcp://127.0.0.1:1502", 1502),
    }

    @classmethod
    def resolve(
        cls,
        settings: Settings,
        *,
        probe: Probe | None = None,
        now: datetime | str | None = None,
        project_root: Path | None = None,
        explicit_battery_binding: bool = False,
    ) -> RuntimeBootstrapResult:
        resolved_at = _coerce_now(now)
        manifest = RuntimeBootstrapManifest(
            profile=settings.bootstrap_profile,
            resolved_at=resolved_at,
        )
        if settings.bootstrap_profile == "none":
            return RuntimeBootstrapResult(settings, manifest, None)

        probe = probe or _probe_tcp
        root = project_root or Path.cwd()
        updates: dict[str, object] = {}
        candidates: list[BootstrapCandidate] = []

        candidates.append(
            cls._home_assistant(
                settings,
                updates,
                probe,
                root,
                explicit_battery_binding=explicit_battery_binding,
            )
        )
        candidates.append(
            cls._simple_endpoint(
                provider_id="zigbee2mqtt",
                current=settings.zigbee2mqtt_url,
                candidate_endpoint=cls._LAB_CANDIDATES["zigbee2mqtt"][0],
                host=cls._LAB_HOST,
                port=cls._LAB_CANDIDATES["zigbee2mqtt"][1],
                setting_name="zigbee2mqtt_url",
                mapping_path=None,
                updates=updates,
                probe=probe,
            )
        )
        candidates.append(
            cls._simple_endpoint(
                provider_id="matter",
                current=settings.matter_server_url,
                candidate_endpoint=cls._LAB_CANDIDATES["matter"][0],
                host=cls._LAB_HOST,
                port=cls._LAB_CANDIDATES["matter"][1],
                setting_name="matter_server_url",
                mapping_path=None,
                updates=updates,
                probe=probe,
            )
        )
        candidates.append(
            cls._modbus(settings, updates, probe, root)
        )
        candidates.append(
            cls._knx(settings, updates, probe, root)
        )

        resolved_settings = settings.model_copy(update=updates) if updates else settings
        manifest = manifest.model_copy(update={"candidates": candidates})
        manifest_path = settings.bootstrap_manifest_path or (
            settings.database_path.parent / "runtime-bootstrap.json"
        )
        _write_manifest(manifest_path, manifest)
        return RuntimeBootstrapResult(resolved_settings, manifest, manifest_path)

    @classmethod
    def _home_assistant(
        cls,
        settings: Settings,
        updates: dict[str, object],
        probe: Probe,
        root: Path,
        *,
        explicit_battery_binding: bool = False,
    ) -> BootstrapCandidate:
        endpoint, port = cls._LAB_CANDIDATES["home_assistant"]
        if settings.home_assistant_url is not None:
            if settings.home_assistant_token is None:
                return BootstrapCandidate(
                    provider_id="home_assistant",
                    endpoint=_safe_endpoint(settings.home_assistant_url),
                    status="skipped",
                    mapping_path=_safe_path(settings.home_assistant_mapping_path),
                    operational_paths=[],
                    reason_code="credentials_missing",
                )
            mapping = settings.home_assistant_mapping_path
            operational_paths: list[str] = []
            # An explicit endpoint still participates in the opt-in lab
            # bootstrap when it is the exact authenticated local HA endpoint.
            # This keeps launcher behavior consistent with automatic endpoint
            # discovery without allowing a remote URL or an unreachable local
            # service to select actuator assets.
            parsed = urlparse(settings.home_assistant_url)
            if (
                cls._is_allowlisted_lab_home_assistant(settings.home_assistant_url)
                and parsed.hostname is not None
                and probe(parsed.hostname, parsed.port or 8123)
            ):
                if mapping is None:
                    mapping = _lab_mapping(root, "home-assistant-lab.json")
                    if mapping is not None:
                        updates["home_assistant_mapping_path"] = mapping
                operational_paths = _auto_configure_lab_operational_paths(
                    settings,
                    updates,
                    root,
                    skip_battery_profile=explicit_battery_binding,
                )
            return BootstrapCandidate(
                provider_id="home_assistant",
                endpoint=_safe_endpoint(settings.home_assistant_url),
                status="configured",
                mapping_path=_safe_path(mapping),
                operational_paths=(
                    operational_paths
                    if operational_paths
                    else _configured_operational_paths(settings)
                ),
                reason_code="explicit_configuration",
            )
        if settings.home_assistant_token is None:
            return BootstrapCandidate(
                provider_id="home_assistant",
                endpoint=endpoint,
                status="skipped",
                reason_code="credentials_missing",
            )
        if not probe(cls._LAB_HOST, port):
            return BootstrapCandidate(
                provider_id="home_assistant",
                endpoint=endpoint,
                status="unavailable",
                reason_code="endpoint_unreachable",
            )
        updates["home_assistant_url"] = endpoint
        mapping = _lab_mapping(root, "home-assistant-lab.json")
        if settings.home_assistant_mapping_path is None and mapping is not None:
            updates["home_assistant_mapping_path"] = mapping
        operational_paths = _auto_configure_lab_operational_paths(
            settings,
            updates,
            root,
            skip_battery_profile=explicit_battery_binding,
        )
        return BootstrapCandidate(
            provider_id="home_assistant",
            endpoint=endpoint,
            status="auto_configured",
            mapping_path=_safe_path(settings.home_assistant_mapping_path or mapping),
            operational_paths=operational_paths,
            reason_code="allowlisted_local_endpoint",
        )

    @staticmethod
    def _is_allowlisted_lab_home_assistant(value: str) -> bool:
        """Recognize only the repository-owned local HA lab endpoint."""

        parsed = urlparse(value)
        try:
            port = parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost"}
            and port == 8123
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and parsed.query == ""
            and parsed.fragment == ""
        )

    @classmethod
    def _simple_endpoint(
        cls,
        *,
        provider_id: str,
        current: str | None,
        candidate_endpoint: str,
        host: str,
        port: int,
        setting_name: str,
        mapping_path: Path | None,
        updates: dict[str, object],
        probe: Probe,
    ) -> BootstrapCandidate:
        if current is not None:
            return BootstrapCandidate(
                provider_id=provider_id,
                endpoint=_safe_endpoint(current),
                status="configured",
                mapping_path=_safe_path(mapping_path),
                reason_code="explicit_configuration",
            )
        if not probe(host, port):
            return BootstrapCandidate(
                provider_id=provider_id,
                endpoint=candidate_endpoint,
                status="unavailable",
                mapping_path=_safe_path(mapping_path),
                reason_code="endpoint_unreachable",
            )
        updates[setting_name] = candidate_endpoint
        return BootstrapCandidate(
            provider_id=provider_id,
            endpoint=candidate_endpoint,
            status="auto_configured",
            mapping_path=_safe_path(mapping_path),
            reason_code="allowlisted_local_endpoint",
        )

    @classmethod
    def _modbus(
        cls,
        settings: Settings,
        updates: dict[str, object],
        probe: Probe,
        root: Path,
    ) -> BootstrapCandidate:
        endpoint = cls._LAB_CANDIDATES["modbus"][0]
        if settings.modbus_host is not None:
            return BootstrapCandidate(
                provider_id="modbus",
                endpoint=f"tcp://{settings.modbus_host}:{settings.modbus_port}",
                status="configured",
                mapping_path=_safe_path(settings.modbus_config_path),
                reason_code="explicit_configuration",
            )
        if not probe(cls._LAB_HOST, 1502):
            return BootstrapCandidate(
                provider_id="modbus",
                endpoint=endpoint,
                status="unavailable",
                reason_code="endpoint_unreachable",
            )
        mapping = _lab_mapping(root, "modbus.json")
        if mapping is None:
            return BootstrapCandidate(
                provider_id="modbus",
                endpoint=endpoint,
                status="unavailable",
                reason_code="mapping_unavailable",
            )
        updates["modbus_host"] = cls._LAB_HOST
        updates["modbus_config_path"] = mapping
        return BootstrapCandidate(
            provider_id="modbus",
            endpoint=endpoint,
            status="auto_configured",
            mapping_path=_safe_path(mapping),
            reason_code="allowlisted_local_endpoint",
        )

    @classmethod
    def _knx(
        cls,
        settings: Settings,
        updates: dict[str, object],
        probe: Probe,
        root: Path,
    ) -> BootstrapCandidate:
        if settings.knx_gateway_host is not None:
            return BootstrapCandidate(
                provider_id="knx",
                endpoint=f"udp://{settings.knx_gateway_host}:{settings.knx_gateway_port}",
                status="configured",
                mapping_path=_safe_path(settings.knx_config_path),
                reason_code="explicit_configuration",
            )
        virtual_host = settings.knx_virtual_host
        if virtual_host is None:
            return BootstrapCandidate(
                provider_id="knx",
                endpoint="udp://windows-knx-virtual:3672",
                status="skipped",
                reason_code="virtual_host_not_declared",
            )
        gateway_host = _derive_local_source_host(virtual_host)
        gateway_port = cls._LAB_KNX_GATEWAY_PORT
        if gateway_host is None or not probe(gateway_host, gateway_port):
            return BootstrapCandidate(
                provider_id="knx",
                endpoint=(
                    f"udp://{gateway_host or virtual_host}:{gateway_port}"
                ),
                status="unavailable",
                reason_code="gateway_unreachable",
            )
        mapping = _lab_mapping(root, "knx-virtual.json")
        if mapping is None:
            return BootstrapCandidate(
                provider_id="knx",
                endpoint=f"udp://{gateway_host}:{gateway_port}",
                status="unavailable",
                reason_code="mapping_unavailable",
            )
        updates["knx_gateway_host"] = gateway_host
        updates["knx_gateway_port"] = gateway_port
        updates["knx_config_path"] = mapping
        return BootstrapCandidate(
            provider_id="knx",
            endpoint=f"udp://{gateway_host}:{gateway_port}",
            status="auto_configured",
            mapping_path=_safe_path(mapping),
            reason_code="allowlisted_local_endpoint",
        )


def _coerce_now(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("bootstrap timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _probe_tcp(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.35):
            return True
    except OSError:
        return False


def _derive_local_source_host(remote_host: str) -> str | None:
    """Return the local interface used to reach KNX Virtual without sending data."""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((remote_host, 3671))
            return str(sock.getsockname()[0])
    except OSError:
        return None


def _lab_mapping(project_root: Path, filename: str) -> Path | None:
    relative = Path("dev") / "lab" / "configs" / filename
    return relative if (project_root / relative).is_file() else None


def _configured_operational_paths(settings: Settings) -> list[str]:
    """Expose configured asset paths without treating them as credentials."""

    paths: list[str] = []
    if settings.battery_dispatch_profile_path is not None:
        paths.append(str(settings.battery_dispatch_profile_path))
    paths.extend(str(path) for path in settings.ev_charging_binding_paths)
    return paths


def _auto_configure_lab_operational_paths(
    settings: Settings,
    updates: dict[str, object],
    project_root: Path,
    *,
    skip_battery_profile: bool = False,
) -> list[str]:
    """Select only the repository-owned lab actuator contracts.

    This helper is reached only after the allowlisted local Home Assistant
    endpoint was probed and its credential was supplied.  It never examines
    provider discovery to invent a route: the two filenames are fixed
    repository assets, and the normal strict binding/qualification gates still
    decide whether a write is executable or production-ready.
    """

    paths: list[str] = []
    if settings.battery_dispatch_profile_path is not None:
        # Preserve an explicitly configured path in diagnostics.  The
        # composition root still rejects combining it with a programmatic
        # binding, so this is not an authority bypass.
        paths.append(str(settings.battery_dispatch_profile_path))
    elif not skip_battery_profile:
        # A caller-provided binding is already the exact runtime authority.
        # When it exists, lab convenience discovery must not reintroduce a
        # second profile or make the manifest diverge from the runtime.
        battery = _lab_mapping(project_root, "dispatchable-battery-lab.json")
        if battery is not None:
            updates["battery_dispatch_profile_path"] = battery
            paths.append(str(battery))

    if settings.ev_charging_binding_paths:
        paths.extend(str(path) for path in settings.ev_charging_binding_paths)
    else:
        ev = _lab_mapping(project_root, "ev-charging-lab.json")
        if ev is not None:
            updates["ev_charging_binding_paths"] = (ev,)
            paths.append(str(ev))
    return paths


def _safe_path(path: Path | None) -> str | None:
    return str(path) if path is not None else None


def _safe_endpoint(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme and parsed.hostname:
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme}://{host}{port}{parsed.path}"
    return value.split("?", 1)[0].split("#", 1)[0][:256]


def _write_manifest(path: Path, manifest: RuntimeBootstrapManifest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(json_manifest(manifest))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def json_manifest(manifest: RuntimeBootstrapManifest) -> str:
    """Serialize without a second JSON dependency in the bootstrap boundary."""

    import json

    return json.dumps(
        manifest.model_dump(mode="json"),
        indent=2,
        sort_keys=True,
    ) + "\n"


__all__ = [
    "BootstrapCandidate",
    "RuntimeBootstrap",
    "RuntimeBootstrapManifest",
    "RuntimeBootstrapResult",
]
