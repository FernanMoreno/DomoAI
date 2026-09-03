"""Read-only validation of the credentialed deployment boundary."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import shlex
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import ValidationError

from domoai.mcp.auth import ClientTokenDocument, StaticBearerTokenVerifier

CheckStatus = Literal["passed", "failed"]
ReportStatus = Literal["passed", "failed"]

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CADDY_HOSTNAME = "{$DOMOAI_CADDY_HOSTNAME}"
_ALLOWED_HTTP_SCHEMES = {"http", "https"}
_ALLOWED_TCP_SCHEMES = {"http", "https", "mqtt", "mqtts", "ws", "wss"}
_DEFAULT_TIMEOUT_SECONDS = 2.0
_MIN_TIMEOUT_SECONDS = 0.1
_MAX_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class DeploymentPreflightRequest:
    """Operator-selected inputs for one side-effect-free preflight."""

    env_file: Path = Path("deploy/gateway.env")
    clients_file: Path = Path("deploy/clients.json")
    compose_file: Path = Path("deploy/compose.yaml")
    caddyfile: Path = Path("deploy/reverse-proxy/Caddyfile")
    network: bool = False
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not _MIN_TIMEOUT_SECONDS <= self.timeout_seconds <= _MAX_TIMEOUT_SECONDS:
            raise ValueError("preflight timeout is outside the server-owned bounds")


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    name: str
    code: str
    status: CheckStatus

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DeploymentPreflightReport:
    status: ReportStatus
    deployment_id: str
    checks: tuple[PreflightCheck, ...]
    schema_version: str = "v1"

    def to_dict(self) -> dict[str, object]:
        return {
            "checks": [check.to_dict() for check in self.checks],
            "deployment_id": self.deployment_id,
            "schema_version": self.schema_version,
            "status": self.status,
        }


def _check(name: str, code: str, passed: bool) -> PreflightCheck:
    return PreflightCheck(name=name, code=code, status="passed" if passed else "failed")


def _safe_regular_file(path: Path, *, reject_symlink: bool = False) -> bool:
    try:
        return (not (reject_symlink and path.is_symlink())) and path.is_file()
    except OSError:
        return False


def _read_text(path: Path) -> str | None:
    if not _safe_regular_file(path):
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _parse_env(path: Path) -> dict[str, str] | None:
    content = _read_text(path)
    if content is None:
        return None
    values: dict[str, str] = {}
    try:
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                return None
            key, raw_value = line.split("=", 1)
            if not _ENV_NAME.fullmatch(key):
                return None
            if key in values:
                return None
            parsed = shlex.split(raw_value, comments=True, posix=True)
            if len(parsed) > 1:
                return None
            values[key] = parsed[0] if parsed else ""
    except (ValueError, UnicodeError):
        return None
    return values


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _valid_url(value: str | None, schemes: set[str]) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError:
        return False
    return parsed.scheme in schemes and parsed.hostname is not None and (
        port is None or 1 <= port <= 65535
    )


def _url_port(parsed_scheme: str, port: int | None) -> int | None:
    if port is not None:
        return port
    return {
        "http": 80,
        "https": 443,
        "mqtt": 1883,
        "mqtts": 8883,
        "ws": 80,
        "wss": 443,
    }.get(parsed_scheme)


def _port(value: str | None, default: int) -> int | None:
    try:
        result = int(value) if value is not None else default
    except ValueError:
        return None
    return result if 1 <= result <= 65535 else None


def _validate_environment(values: dict[str, str]) -> bool:
    host = values.get("DOMOAI_MCP_HOST", "127.0.0.1")
    public_url = urlparse(values.get("DOMOAI_MCP_PUBLIC_URL", "http://127.0.0.1:8000"))
    path = values.get("DOMOAI_MCP_PATH", "/mcp")
    if (
        not host
        or len(path) < 2
        or not path.startswith("/")
        or any(character.isspace() for character in path)
        or "?" in path
        or "#" in path
    ):
        return False
    if public_url.scheme not in _ALLOWED_HTTP_SCHEMES or public_url.hostname is None:
        return False
    try:
        public_port = public_url.port
    except ValueError:
        return False
    if public_port is not None and not 1 <= public_port <= 65535:
        return False
    if public_url.path not in {"", "/"} or public_url.query or public_url.fragment:
        return False
    if not _is_loopback(host):
        if public_url.scheme != "https" or not values.get("DOMOAI_MCP_CLIENT_TOKEN_FILE"):
            return False
    caddy_hostname = values.get("DOMOAI_CADDY_HOSTNAME")
    if not caddy_hostname or public_url.hostname != caddy_hostname:
        return False
    if values.get("DOMOAI_KNX_GATEWAY_HOST") is not None and not values.get(
        "DOMOAI_KNX_CONFIG_PATH"
    ):
        return False
    if values.get("DOMOAI_MODBUS_HOST") is not None and not values.get("DOMOAI_MODBUS_CONFIG_PATH"):
        return False
    for key in (
        "DOMOAI_HOME_ASSISTANT_URL",
        "DOMOAI_ZIGBEE2MQTT_URL",
        "DOMOAI_MATTER_SERVER_URL",
    ):
        if key in values and not _valid_url(values[key], _ALLOWED_TCP_SCHEMES):
            return False
    return _port(values.get("DOMOAI_MCP_PORT"), 8000) is not None


def _service_block(compose: str, service: str) -> str | None:
    match = re.search(
        rf"(?ms)^  {re.escape(service)}:\s*$\n"
        r"(.*?)(?=^  [A-Za-z0-9_-]+:\s*$|^volumes:\s*$|\Z)",
        compose,
    )
    return match.group(1) if match else None


def _validate_compose(compose: str, gateway_port: int = 8124) -> bool:
    required = ("mqtt", "homeassistant", "gateway", "proxy")
    blocks = {name: _service_block(compose, name) for name in required}
    if any(block is None for block in blocks.values()):
        return False
    gateway = blocks["gateway"] or ""
    proxy = blocks["proxy"] or ""
    if re.search(r"(?m)^    ports:", gateway):
        return False
    if not re.search(
        rf"(?ms)^    expose:\s*(?:\[[^\]]*{gateway_port}[^\]]*\]|$\n\s*-\s*['\"]?{gateway_port})",
        gateway,
    ):
        return False
    if "/run/secrets/mcp-clients.json:ro" not in gateway:
        return False
    if "domoai-data:/app/data" not in gateway:
        return False
    if "image: caddy:" not in proxy or "/etc/caddy/Caddyfile:ro" not in proxy:
        return False
    return ":80:80" in proxy and ":443:443" in proxy


def _validate_proxy(caddy: str, values: dict[str, str]) -> bool:
    mcp_path = values.get("DOMOAI_MCP_PATH", "/mcp")
    required = (
        _CADDY_HOSTNAME,
        f"path {mcp_path} {mcp_path}/*",
        "path /healthz /readyz",
        "respond 404",
    )
    if any(item not in caddy for item in required):
        return False
    if not re.search(r"(?m)^\s*tls(?:\s|$)", caddy):
        return False
    if caddy.count("reverse_proxy gateway:8124") != 2:
        return False
    if values.get("DOMOAI_CADDY_HOSTNAME") is None:
        return False
    fallback = caddy.split("handle {", 1)[-1]
    return "reverse_proxy gateway:8124" not in fallback


def _host_path(raw: str, base: Path) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else base / path


def _compose_mount_source(compose: str, target: str, values: dict[str, str]) -> str | None:
    match = re.search(rf"(?m)^\s*-\s*(.+):{re.escape(target)}:ro\s*$", compose)
    if match is None:
        return None
    source = match.group(1).strip()
    variable = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]+))?\}", source)
    if variable is None:
        return source
    key, default = variable.groups()
    return values.get(key, default)


def _validate_referenced_files(
    values: dict[str, str], env_file: Path, compose: str, clients_file: Path
) -> bool:
    keys = (
        "DOMOAI_MCP_CLIENT_TOKEN_FILE_HOST",
        "DOMOAI_KNX_CONFIG_PATH_HOST",
        "DOMOAI_MODBUS_CONFIG_PATH_HOST",
        "DOMOAI_HOME_ASSISTANT_MAPPING_PATH",
        "DOMOAI_MQTT_CA_CERT_PATH",
        "DOMOAI_MQTT_CLIENT_CERT_PATH",
        "DOMOAI_MQTT_CLIENT_KEY_PATH",
    )
    for key in keys:
        if key in values and not _safe_regular_file(_host_path(values[key], env_file.parent)):
            return False
    clients_source = _compose_mount_source(
        compose, "/run/secrets/mcp-clients.json", values
    )
    if clients_source is None:
        return False
    try:
        if _host_path(clients_source, env_file.parent).resolve() != clients_file.resolve():
            return False
    except OSError:
        return False
    if values.get("DOMOAI_KNX_CONFIG_PATH"):
        knx_source = _compose_mount_source(
            compose, "/app/config/knx-config.json", values
        )
        if knx_source is None or not _safe_regular_file(_host_path(knx_source, env_file.parent)):
            return False
    cert = values.get("DOMOAI_MQTT_CLIENT_CERT_PATH") is not None
    client_key = values.get("DOMOAI_MQTT_CLIENT_KEY_PATH") is not None
    return cert == client_key


def _client_checks(path: Path) -> tuple[PreflightCheck, PreflightCheck]:
    auth_name = "client_authentication"
    usable_name = "usable_clients"
    if not _safe_regular_file(path, reject_symlink=True):
        return (
            _check(auth_name, "clients_file_invalid", False),
            _check(usable_name, "clients_unusable", False),
        )
    try:
        verifier = StaticBearerTokenVerifier.from_file(path)
        document = ClientTokenDocument.model_validate_json(path.read_text(encoding="utf-8"))
        del verifier
    except (OSError, UnicodeError, ValueError, ValidationError):
        return (
            _check(auth_name, "clients_file_invalid", False),
            _check(usable_name, "clients_unusable", False),
        )
    now = datetime.now(UTC)
    usable = any(
        record.enabled and (record.expires_at is None or record.expires_at > now)
        for record in document.clients
    )
    return (
        _check(auth_name, "clients_file_valid", True),
        _check(usable_name, "clients_usable", usable),
    )


def _dependency_endpoints(values: dict[str, str]) -> list[tuple[str, str, int]]:
    endpoints: list[tuple[str, str, int]] = []
    for label, key in (
        ("home_assistant", "DOMOAI_HOME_ASSISTANT_URL"),
        ("zigbee2mqtt", "DOMOAI_ZIGBEE2MQTT_URL"),
        ("matter", "DOMOAI_MATTER_SERVER_URL"),
    ):
        raw = values.get(key)
        if raw:
            parsed = urlparse(raw)
            try:
                port = _url_port(parsed.scheme, parsed.port)
            except ValueError:
                port = None
            if parsed.hostname is not None and port is not None:
                endpoints.append((label, parsed.hostname, port))
    for label, host_key, port_key, default_port in (
        ("knx", "DOMOAI_KNX_GATEWAY_HOST", "DOMOAI_KNX_GATEWAY_PORT", 3671),
        ("modbus", "DOMOAI_MODBUS_HOST", "DOMOAI_MODBUS_PORT", 502),
    ):
        host = values.get(host_key)
        if host:
            port = _port(values.get(port_key), default_port)
            if port is not None:
                endpoints.append((label, host, port))
    return endpoints


async def _probe(host: str, port: int, timeout_seconds: float) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout_seconds
        )
    except (OSError, TimeoutError):
        return False
    del reader
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def _network_check(values: dict[str, str], timeout_seconds: float) -> PreflightCheck:
    endpoints = _dependency_endpoints(values)
    results = await asyncio.gather(
        *(_probe(host, port, timeout_seconds) for _, host, port in endpoints)
    )
    reachable = all(results)
    return _check(
        "network_dependencies",
        "network_dependencies_reachable" if reachable else "dependency_unavailable",
        reachable,
    )


async def run_preflight(request: DeploymentPreflightRequest) -> DeploymentPreflightReport:
    """Run all selected checks without changing process or deployment state."""

    checks: list[PreflightCheck] = []
    values = _parse_env(request.env_file)
    deployment_id = "unknown"
    if values is None:
        checks.append(_check("environment", "artifact_unavailable", False))
        return DeploymentPreflightReport("failed", deployment_id, tuple(checks))

    candidate_id = values.get("DOMOAI_MCP_DEPLOYMENT_ID", "default")
    deployment_id = (
        candidate_id if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", candidate_id) else "unknown"
    )
    environment_valid = _validate_environment(values)
    checks.append(
        _check(
            "environment",
            "environment_valid" if environment_valid else "environment_invalid",
            environment_valid,
        )
    )
    client_auth, usable_clients = _client_checks(request.clients_file)
    checks.extend((client_auth, usable_clients))
    compose = _read_text(request.compose_file)
    configured_port = _port(values.get("DOMOAI_MCP_PORT"), 8000) or 8000
    compose_valid = compose is not None and _validate_compose(compose, configured_port)
    checks.append(
        _check(
            "compose_boundary",
            "compose_boundary_valid" if compose_valid else "compose_boundary_invalid",
            compose_valid,
        )
    )
    caddy = _read_text(request.caddyfile)
    proxy_valid = caddy is not None and _validate_proxy(caddy, values)
    checks.append(
        _check(
            "proxy_boundary",
            "proxy_boundary_valid" if proxy_valid else "proxy_boundary_invalid",
            proxy_valid,
        )
    )
    files_valid = _validate_referenced_files(
        values, request.env_file, compose or "", request.clients_file
    )
    checks.append(
        _check(
            "referenced_files",
            "referenced_files_valid" if files_valid else "referenced_file_unavailable",
            files_valid,
        )
    )

    static_valid = all(check.status == "passed" for check in checks)
    if request.network and static_valid:
        checks.append(await _network_check(values, request.timeout_seconds))
    status: ReportStatus = (
        "passed" if all(check.status == "passed" for check in checks) else "failed"
    )
    return DeploymentPreflightReport(status, deployment_id, tuple(checks))
