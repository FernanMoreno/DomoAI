"""Testable command and environment boundary for the local Compose lab."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class LabRunnerError(RuntimeError):
    """Raised for invalid local-lab configuration or command execution."""


class BridgeSupervisorPort(Protocol):
    def start(self) -> int: ...

    def status(self) -> int: ...

    def stop(self) -> int: ...


SERVICE_NAMES = frozenset(
    {
        "mqtt",
        "zigbee2mqtt",
        "modbus",
        "battery",
        "ev-charger",
        "water-meter",
        "thermal",
        "knx-gateway",
        "homeassistant",
        "matter-server",
        "knx-bridge",
    }
)
DEFAULT_UP_SERVICES = ("mqtt", "zigbee2mqtt", "modbus")
SERVICE_PROFILES = {
    "battery": "battery",
    "ev-charger": "ev-charger",
    "water-meter": "water-meter",
    "thermal": "thermal",
    "homeassistant": "homeassistant",
    "matter-server": "matter",
}
FIXTURE_SMOKE_TESTS = (
    "tests/integration/test_virtual_lab_assets.py",
    "tests/integration/test_virtual_lab_smoke_configuration.py",
    "tests/integration/test_zigbee2mqtt_fixture.py",
    "tests/integration/test_modbus_fixture.py",
    "tests/unit/lab/test_battery_simulator.py",
    "tests/unit/lab/test_ev_charging_simulator.py",
    "tests/unit/lab/test_water_consumption_simulator.py",
    "tests/unit/lab/test_thermal_simulator.py",
    "tests/integration/test_matter_server_fixture.py",
    "tests/integration/test_knx_fixture.py",
    "tests/integration/test_home_assistant_provider_runtime.py",
    "tests/integration/test_multi_adapter_runtime.py",
    "tests/integration/test_solar_self_consumption_mcp.py",
)
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

CommandExecutor = Callable[[Sequence[str], Mapping[str, str]], int]


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a small non-shell env format without expanding or executing values."""

    if not path.exists():
        return {}
    if not path.is_file():
        raise LabRunnerError(f"lab env file is not a regular file: {path}")

    values: dict[str, str] = {}
    for line_number, original in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise LabRunnerError(f"invalid lab env line {line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not _ENV_KEY.fullmatch(key):
            raise LabRunnerError(f"invalid lab env key on line {line_number}")
        value = value.strip()
        if value[:1] in {"'", '"'}:
            try:
                parsed = shlex.split(value, comments=False, posix=True)
            except ValueError as error:
                raise LabRunnerError(f"invalid quoted value on env line {line_number}") from error
            if len(parsed) != 1:
                raise LabRunnerError(f"invalid quoted value on env line {line_number}")
            value = parsed[0]
        values[key] = value
    return values


@dataclass(frozen=True)
class LabConfig:
    repo_root: Path
    compose_file: Path | None = None
    env_file: Path | None = None
    docker_executable: str = "docker"
    python_executable: str = sys.executable
    project_name: str = "domoai-lab"

    def resolved_compose_file(self) -> Path:
        compose_file = self.compose_file or self.repo_root / "dev" / "lab" / "compose.yaml"
        return compose_file.resolve()

    def resolved_env_file(self) -> Path:
        env_file = self.env_file or self.repo_root / "dev" / "lab" / ".env"
        return env_file.resolve()


class LabRunner:
    """Build and execute allowlisted local lab commands."""

    def __init__(
        self,
        config: LabConfig,
        execute: CommandExecutor | None = None,
        bridge_supervisor: BridgeSupervisorPort | None = None,
    ) -> None:
        self.config = config
        self._execute = execute or self._execute_process
        self._bridge_supervisor = bridge_supervisor

    def up(self, services: Sequence[str] = DEFAULT_UP_SERVICES) -> int:
        selected = self._validate_services(services)
        compose_services = tuple(service for service in selected if service != "knx-bridge")
        if compose_services:
            compose_result = self._run_compose(
                ("up", "-d", "--build", *compose_services),
                include_local_env=True,
                services=compose_services,
            )
            if compose_result != 0:
                return compose_result
        if "knx-bridge" in selected:
            return self._bridge().start()
        return 0

    def status(self, services: Sequence[str] = ()) -> int:
        selected = self._validate_services(services)
        compose_services = tuple(service for service in selected if service != "knx-bridge")
        result = 0
        if compose_services or not selected:
            result = self._run_compose(
                ("ps", *compose_services), include_local_env=True, services=compose_services
            )
        if "knx-bridge" in selected:
            result = max(result, self._bridge().status())
        return result

    def down(self, *, remove_volumes: bool = False) -> int:
        command: tuple[str, ...] = ("down", "--volumes") if remove_volumes else ("down",)
        bridge_result = self._bridge().stop()
        compose_result = self._run_compose(command, include_local_env=True, services=())
        return max(bridge_result, compose_result)

    def smoke(self) -> int:
        env = self._deterministic_environment()
        argv = [self.config.python_executable, "-m", "pytest", "-q", *FIXTURE_SMOKE_TESTS]
        return self._execute(argv, env)

    def _run_compose(
        self,
        command: Sequence[str],
        *,
        include_local_env: bool,
        services: Sequence[str],
    ) -> int:
        compose_file = self.config.resolved_compose_file()
        if not compose_file.is_file():
            raise LabRunnerError(f"lab Compose file not found: {compose_file}")
        profiles = sorted(
            {SERVICE_PROFILES[service] for service in services if service in SERVICE_PROFILES}
        )
        compose_argument = self._compose_argument(compose_file)
        argv = [
            self.config.docker_executable,
            "compose",
            "-p",
            self.config.project_name,
            "-f",
            compose_argument,
        ]
        for profile in profiles:
            argv.extend(("--profile", profile))
        argv.extend(command)
        env = self._environment(include_local_env=include_local_env)
        return self._execute(argv, env)

    def _bridge(self) -> BridgeSupervisorPort:
        if self._bridge_supervisor is None:
            from domoai.lab.bridge_supervisor import BridgeSupervisor, BridgeSupervisorConfig

            environment = self._environment(include_local_env=True)
            try:
                bridge_config = BridgeSupervisorConfig.from_environment(
                    self.config.repo_root,
                    environment,
                    python_executable=self.config.python_executable,
                )
            except ValueError as error:
                raise LabRunnerError(str(error)) from error
            self._bridge_supervisor = BridgeSupervisor(bridge_config)
        return self._bridge_supervisor

    def _compose_argument(self, compose_file: Path) -> str:
        if not self.config.docker_executable.lower().endswith(".exe"):
            return str(compose_file)
        try:
            relative = compose_file.relative_to(self.config.repo_root.resolve())
        except ValueError:
            return str(compose_file)
        return relative.as_posix()

    def _validate_services(self, services: Sequence[str]) -> tuple[str, ...]:
        selected = tuple(services)
        unknown = sorted(set(selected) - SERVICE_NAMES)
        if unknown:
            raise LabRunnerError(f"unknown lab service(s): {', '.join(unknown)}")
        return selected

    def _environment(self, *, include_local_env: bool) -> dict[str, str]:
        environment = dict(os.environ)
        if include_local_env:
            environment.update(parse_env_file(self.config.resolved_env_file()))
        return environment

    def _deterministic_environment(self) -> dict[str, str]:
        environment = self._environment(include_local_env=False)
        for key in tuple(environment):
            if key.startswith("DOMOAI_"):
                del environment[key]
        return environment

    def _execute_process(self, argv: Sequence[str], env: Mapping[str, str]) -> int:
        try:
            completed = subprocess.run(
                list(argv),
                cwd=str(self.config.repo_root.resolve()),
                env=dict(env),
                check=False,
            )
        except OSError as error:
            executable = argv[0] if argv else "command"
            raise LabRunnerError(f"unable to execute lab command '{executable}'") from error
        return completed.returncode
