"""Supervise the WSL-side bridge used by the KNX Virtual lab."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol


class BridgeState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"
    STOPPED = "stopped"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class BridgeStatus:
    state: BridgeState
    schema_version: str = "v1"
    pid: int | None = None
    started_at: str | None = None
    updated_at: str = field(default_factory=_utc_now)
    last_state_at: str | None = None
    knx_readback_at: str | None = None
    knx_readback_ok: bool | None = None
    mapping_path: str | None = None
    mapping_digest: str | None = None
    knx_host: str | None = None
    knx_port: int = 3672
    error_code: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "state": self.state.value,
            "pid": self.pid,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "last_state_at": self.last_state_at,
            "knx_readback_at": self.knx_readback_at,
            "knx_readback_ok": self.knx_readback_ok,
            "mapping_path": self.mapping_path,
            "mapping_digest": self.mapping_digest,
            "knx_host": self.knx_host,
            "knx_port": self.knx_port,
            "error_code": self.error_code,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> BridgeStatus:
        if payload.get("schema_version") != "v1":
            raise ValueError("unsupported bridge status schema")
        try:
            state = BridgeState(str(payload["state"]))
        except (KeyError, ValueError) as error:
            raise ValueError("invalid bridge status state") from error
        pid = payload.get("pid")
        if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0):
            raise ValueError("invalid bridge status pid")
        port = payload.get("knx_port", 3672)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("invalid bridge status port")
        readback_at = _optional_string(payload.get("knx_readback_at"))
        readback_ok = payload.get("knx_readback_ok")
        if readback_ok is not None and not isinstance(readback_ok, bool):
            raise ValueError("invalid bridge readback status")
        return cls(
            state=state,
            schema_version="v1",
            pid=pid,
            started_at=_optional_string(payload.get("started_at")),
            updated_at=_required_string(payload.get("updated_at")),
            last_state_at=_optional_string(payload.get("last_state_at")),
            knx_readback_at=readback_at,
            knx_readback_ok=readback_ok,
            mapping_path=_optional_string(payload.get("mapping_path")),
            mapping_digest=_optional_string(payload.get("mapping_digest")),
            knx_host=_optional_string(payload.get("knx_host")),
            knx_port=port,
            error_code=_optional_string(payload.get("error_code")),
            message=_optional_string(payload.get("message")),
        )


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid bridge status string")
    return value


def _required_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("missing bridge status timestamp")
    return value


class BridgeStatusStore:
    """Atomically publish and safely parse one bridge status file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, status: BridgeStatus) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(status.to_dict(), sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def read(self) -> BridgeStatus | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            return BridgeStatus.from_dict(payload)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None


def mapping_digest(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


@dataclass(frozen=True)
class BridgeSupervisorConfig:
    repo_root: Path
    python_executable: str = sys.executable
    bridge_script: Path | None = None
    mapping_path: Path | None = None
    state_dir: Path | None = None
    knx_host: str | None = None
    knx_port: int = 3672
    knx_route_back: bool = False
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_topic: str = "domoai/battery"
    timeout_seconds: float = 5.0
    readiness_timeout_seconds: float = 15.0
    stop_timeout_seconds: float = 10.0
    verify_knx_readback: bool = True
    environment: Mapping[str, str] | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_environment(
        cls, repo_root: Path, environment: Mapping[str, str], *, python_executable: str
    ) -> BridgeSupervisorConfig:
        return cls(
            repo_root=repo_root,
            python_executable=python_executable,
            mapping_path=Path(
                environment.get(
                    "DOMOAI_KNX_BRIDGE_MAPPING_PATH",
                    "dev/lab/configs/knx-battery-virtual.json",
                )
            ),
            state_dir=Path(environment.get("DOMOAI_KNX_BRIDGE_STATE_DIR", ".lab-state")),
            knx_host=environment.get("DOMOAI_KNX_GATEWAY_HOST"),
            knx_port=_integer(environment, "DOMOAI_KNX_GATEWAY_PORT", 3672),
            knx_route_back=_boolean(environment, "DOMOAI_KNX_ROUTE_BACK", False),
            mqtt_host=environment.get("DOMOAI_BATTERY_MQTT_HOST", "127.0.0.1"),
            mqtt_port=_integer(environment, "DOMOAI_BATTERY_MQTT_PORT", 1883),
            mqtt_topic=environment.get("DOMOAI_BATTERY_MQTT_TOPIC", "domoai/battery"),
            timeout_seconds=_float(environment, "DOMOAI_KNX_TIMEOUT_SECONDS", 5.0),
            readiness_timeout_seconds=_float(
                environment, "DOMOAI_KNX_BRIDGE_READINESS_TIMEOUT_SECONDS", 15.0
            ),
            stop_timeout_seconds=_float(
                environment, "DOMOAI_KNX_BRIDGE_STOP_TIMEOUT_SECONDS", 10.0
            ),
            verify_knx_readback=_boolean(
                environment, "DOMOAI_KNX_BRIDGE_VERIFY_READBACK", True
            ),
            environment=dict(environment),
        )

    @property
    def resolved_bridge_script(self) -> Path:
        return _resolve_under_root(
            self.repo_root, self.bridge_script or Path("dev/lab/battery/knx_bridge.py")
        )

    @property
    def resolved_mapping_path(self) -> Path:
        return _resolve_under_root(
            self.repo_root,
            self.mapping_path or Path("dev/lab/configs/knx-battery-virtual.json"),
        )

    @property
    def resolved_state_dir(self) -> Path:
        return _resolve_under_root(self.repo_root, self.state_dir or Path(".lab-state"))

    @property
    def status_path(self) -> Path:
        return self.resolved_state_dir / "knx-battery-bridge.json"

    @property
    def log_path(self) -> Path:
        return self.resolved_state_dir / "knx-battery-bridge.log"

    def validate(self) -> None:
        if not self.python_executable.strip():
            raise ValueError("python executable is required")
        if not self.knx_host or not self.knx_host.strip():
            raise ValueError("DOMOAI_KNX_GATEWAY_HOST is required")
        if not 1 <= self.knx_port <= 65535:
            raise ValueError("KNX gateway port must be between 1 and 65535")
        if not 1 <= self.mqtt_port <= 65535:
            raise ValueError("MQTT port must be between 1 and 65535")
        if not self.resolved_bridge_script.is_file():
            raise ValueError("KNX bridge script is missing")
        if not self.resolved_mapping_path.is_file():
            raise ValueError("KNX battery mapping is missing")
        if not self.mqtt_topic.strip():
            raise ValueError("MQTT topic is required")
        for value, name in (
            (self.timeout_seconds, "KNX timeout"),
            (self.readiness_timeout_seconds, "bridge readiness timeout"),
            (self.stop_timeout_seconds, "bridge stop timeout"),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")


def _integer(environment: Mapping[str, str], key: str, default: int) -> int:
    try:
        return int(environment.get(key, str(default)))
    except ValueError as error:
        raise ValueError(f"{key} must be an integer") from error


def _float(environment: Mapping[str, str], key: str, default: float) -> float:
    try:
        return float(environment.get(key, str(default)))
    except ValueError as error:
        raise ValueError(f"{key} must be numeric") from error


def _boolean(environment: Mapping[str, str], key: str, default: bool) -> bool:
    value = environment.get(key, "1" if default else "0").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{key} must be boolean")


def _resolve_under_root(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


class ProcessHandle(Protocol):
    pid: int


ProcessFactory = Callable[[Sequence[str], Mapping[str, str], Path], ProcessHandle]
ProcessProbe = Callable[[int], bool]
SignalProcess = Callable[[int, int], None]


class BridgeSupervisor:
    """Own one bridge child and never manage a process it cannot identify."""

    def __init__(
        self,
        config: BridgeSupervisorConfig,
        *,
        process_factory: ProcessFactory | None = None,
        process_probe: ProcessProbe | None = None,
        signal_process: SignalProcess | None = None,
        readiness_probe: Callable[[], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._process_factory = process_factory or self._spawn
        self._process_probe = process_probe or self._default_process_probe
        self._signal_process = signal_process or os.kill
        self._readiness_probe = (
            readiness_probe
            if readiness_probe is not None
            else self._probe_knx_readback
            if config.verify_knx_readback
            else None
        )
        self._sleep = sleep
        self._monotonic = monotonic
        self._store = BridgeStatusStore(config.status_path)

    def start(self) -> int:
        try:
            self.config.validate()
        except ValueError as error:
            self._print_failure("configuration_invalid", str(error))
            return 2

        existing = self._store.read()
        if existing is not None and existing.pid is not None and self._process_probe(existing.pid):
            expected = self._base_status(BridgeState.STARTING, existing.pid, existing.started_at)
            if (
                existing.mapping_digest != expected.mapping_digest
                or existing.mapping_path != expected.mapping_path
                or existing.knx_host != expected.knx_host
                or existing.knx_port != expected.knx_port
            ):
                self._print_failure(
                    "configuration_conflict",
                    "a supervised bridge is already running with different configuration",
                )
                return 2
            return self._wait_for_ready(existing.pid, existing)

        if existing is not None and existing.state is BridgeState.READY:
            self._write(
                replace(
                    existing,
                    state=BridgeState.FAILED,
                    pid=None,
                    updated_at=_utc_now(),
                    error_code="stale_process",
                    message="supervised bridge process is not alive",
                )
            )

        started_at = _utc_now()
        self._write(self._base_status(BridgeState.STARTING, None, started_at))
        try:
            process = self._process_factory(
                self._command(), self._environment(), self.config.log_path
            )
        except (OSError, ValueError) as error:
            self._write(
                replace(
                    self._base_status(BridgeState.FAILED, None, started_at),
                    error_code="spawn_failed",
                    message=f"unable to start supervised bridge ({type(error).__name__})",
                )
            )
            self._print_status(
                self._store.read() or self._base_status(BridgeState.FAILED, None, started_at),
                2,
            )
            return 2

        observed = self._store.read()
        status = (
            observed
            if observed is not None and observed.pid == process.pid
            else self._base_status(BridgeState.STARTING, process.pid, started_at)
        )
        self._write(status)
        return self._wait_for_ready(process.pid, status)

    def status(self) -> int:
        status = self._store.read()
        if status is None:
            self._print_line("status=stopped error_code=not_started")
            return 1
        if status.pid is None or not self._process_probe(status.pid):
            status = replace(
                status,
                state=BridgeState.FAILED,
                pid=None,
                updated_at=_utc_now(),
                error_code="stale_process",
                message="supervised bridge process is not alive",
            )
            self._write(status)
            self._print_status(status, 1)
            return 1
        if self._should_probe_readback(status):
            return self._check_knx_readback(status)
        code = 0 if status.state is BridgeState.READY else 1
        self._print_status(status, code)
        return code

    def stop(self) -> int:
        status = self._store.read()
        if status is None or status.pid is None:
            if status is None:
                self._print_line("status=stopped error_code=not_running")
                return 0
            stopped = replace(status, state=BridgeState.STOPPED, pid=None, updated_at=_utc_now())
            self._write(stopped)
            self._print_status(stopped, 0)
            return 0
        pid = status.pid
        if not self._process_probe(pid):
            stopped = replace(
                status,
                state=BridgeState.STOPPED,
                pid=None,
                updated_at=_utc_now(),
                error_code=None,
                message="bridge is not running",
            )
            self._write(stopped)
            self._print_status(stopped, 0)
            return 0

        try:
            self._signal_process(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        if not self._wait_until_gone(pid, self.config.stop_timeout_seconds):
            if self._process_probe(pid):
                try:
                    self._signal_process(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if not self._wait_until_gone(pid, min(self.config.stop_timeout_seconds, 2.0)):
                failed = replace(
                    status,
                    state=BridgeState.FAILED,
                    updated_at=_utc_now(),
                    error_code="stop_timeout",
                    message="supervised bridge did not stop",
                )
                self._write(failed)
                self._print_status(failed, 1)
                return 1

        stopped = replace(
            status,
            state=BridgeState.STOPPED,
            pid=None,
            updated_at=_utc_now(),
            error_code=None,
            message="bridge stopped",
        )
        self._write(stopped)
        self._print_status(stopped, 0)
        return 0

    def _wait_for_ready(self, pid: int, initial: BridgeStatus) -> int:
        deadline = self._monotonic() + self.config.readiness_timeout_seconds
        current = initial
        while True:
            observed = self._store.read()
            if observed is not None and observed.pid == pid:
                current = observed
                if self._should_probe_readback(observed) and self._process_probe(pid):
                    return self._check_knx_readback(observed)
                if observed.state in {BridgeState.FAILED, BridgeState.STOPPED}:
                    self._print_status(observed, 1)
                    return 1
            if not self._process_probe(pid):
                failed = replace(
                    current,
                    state=BridgeState.FAILED,
                    pid=None,
                    updated_at=_utc_now(),
                    error_code="process_exited",
                    message="supervised bridge process exited before ready",
                )
                self._write(failed)
                self._print_status(failed, 1)
                return 1
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                self._print_status(current, 1)
                return 1
            self._sleep(min(0.1, remaining))

    @staticmethod
    def _should_probe_readback(status: BridgeStatus) -> bool:
        return status.state is BridgeState.READY or (
            status.state is BridgeState.DEGRADED
            and status.last_state_at is not None
            and status.error_code in {None, "knx_readback_unavailable"}
        )

    def _wait_until_gone(self, pid: int, timeout: float) -> bool:
        deadline = self._monotonic() + timeout
        while self._process_probe(pid):
            if self._monotonic() >= deadline:
                return False
            self._sleep(min(0.1, max(deadline - self._monotonic(), 0.0)))
        return True

    def _check_knx_readback(self, status: BridgeStatus) -> int:
        if self._readiness_probe is None:
            self._print_status(status, 0)
            return 0
        checked_at = _utc_now()
        try:
            verified = self._readiness_probe()
        except Exception:
            verified = False
        if verified:
            ready = replace(
                status,
                state=BridgeState.READY,
                updated_at=checked_at,
                knx_readback_at=checked_at,
                knx_readback_ok=True,
                error_code=None,
                message="battery state projected to KNX and readback verified",
            )
            self._write(ready)
            self._print_status(ready, 0)
            return 0
        degraded = replace(
            status,
            state=BridgeState.DEGRADED,
            updated_at=checked_at,
            knx_readback_at=checked_at,
            knx_readback_ok=False,
            error_code="knx_readback_unavailable",
            message="KNX bridge is connected but battery readback is unavailable",
        )
        self._write(degraded)
        self._print_status(degraded, 1)
        return 1

    def _base_status(
        self, state: BridgeState, pid: int | None, started_at: str | None
    ) -> BridgeStatus:
        mapping = self.config.resolved_mapping_path
        try:
            digest = mapping_digest(mapping)
        except OSError:
            digest = None
        try:
            mapping_name = mapping.relative_to(self.config.repo_root).as_posix()
        except ValueError:
            mapping_name = mapping.name
        return BridgeStatus(
            state=state,
            pid=pid,
            started_at=started_at,
            mapping_path=mapping_name,
            mapping_digest=digest,
            knx_host=self.config.knx_host,
            knx_port=self.config.knx_port,
            updated_at=_utc_now(),
        )

    def _command(self) -> tuple[str, ...]:
        command = (
            self.config.python_executable,
            str(self.config.resolved_bridge_script),
            "--mapping",
            self._mapping_argument(),
            "--mqtt-host",
            self.config.mqtt_host,
            "--mqtt-port",
            str(self.config.mqtt_port),
            "--mqtt-topic",
            self.config.mqtt_topic,
            "--knx-host",
            str(self.config.knx_host),
            "--knx-port",
            str(self.config.knx_port),
            "--timeout",
            str(self.config.timeout_seconds),
            "--status-file",
            str(self.config.status_path),
        )
        return command + (
            ("--knx-route-back",) if self.config.knx_route_back else ("--no-knx-route-back",)
        )

    def _mapping_argument(self) -> str:
        mapping = self.config.resolved_mapping_path
        try:
            return mapping.relative_to(self.config.repo_root).as_posix()
        except ValueError:
            return str(mapping)

    def _environment(self) -> dict[str, str]:
        environment = dict(self.config.environment or os.environ)
        environment["PYTHONUNBUFFERED"] = "1"
        return environment

    def _spawn(
        self, argv: Sequence[str], environment: Mapping[str, str], log_path: Path
    ) -> subprocess.Popen[Any]:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log:
            return subprocess.Popen(
                list(argv),
                cwd=str(self.config.repo_root),
                env=dict(environment),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    def _default_process_probe(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except (PermissionError, ProcessLookupError, OSError):
            return False
        proc_cmdline = Path(f"/proc/{pid}/cmdline")
        try:
            command = proc_cmdline.read_bytes().split(b"\0")
        except OSError:
            return False
        return str(self.config.resolved_bridge_script).encode() in command

    def _probe_knx_readback(self) -> bool:
        import asyncio

        try:
            return asyncio.run(
                asyncio.wait_for(
                    self._probe_knx_readback_async(),
                    timeout=self.config.readiness_timeout_seconds,
                )
            )
        except Exception:
            return False

    async def _probe_knx_readback_async(self) -> bool:
        from domoai.adapters.knx.config import load_mapping
        from domoai.adapters.knx.transport import XknxTransport

        if self.config.knx_host is None:
            return False
        mapping = load_mapping(self.config.resolved_mapping_path)
        state_groups = {
            binding.state_group_address: binding.dpt
            for entity in mapping.entities
            if entity.semantic_type == "energy"
            for binding in entity.capabilities
        }
        if not state_groups:
            return False
        transport = XknxTransport(
            self.config.knx_host,
            gateway_port=self.config.knx_port,
            route_back=self.config.knx_route_back,
            timeout=self.config.timeout_seconds,
            group_dpts=state_groups,
        )
        try:
            await transport.connect()
            for address, dpt in state_groups.items():
                if await transport.read_group(address, dpt) is None:
                    return False
            return True
        except Exception:
            return False
        finally:
            await transport.disconnect()

    def _write(self, status: BridgeStatus) -> None:
        self._store.write(status)

    def _print_failure(self, code: str, message: str) -> None:
        self._print_line(f"status=failed error_code={code} message={message}")

    def _print_status(self, status: BridgeStatus, _code: int) -> None:
        fields = [f"status={status.state.value}"]
        if status.pid is not None:
            fields.append(f"pid={status.pid}")
        if status.error_code:
            fields.append(f"error_code={status.error_code}")
        if status.message:
            fields.append(f"message={status.message}")
        self._print_line(" ".join(fields))

    @staticmethod
    def _print_line(message: str) -> None:
        print(f"knx-bridge {message}", flush=True)
