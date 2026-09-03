from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from domoai.lab.runner import (
    DEFAULT_UP_SERVICES,
    SERVICE_NAMES,
    LabConfig,
    LabRunner,
    LabRunnerError,
    parse_env_file,
)


class FakeBridgeSupervisor:
    def __init__(self, events: list[str], *, start_result: int = 0) -> None:
        self.events = events
        self.start_result = start_result

    def start(self) -> int:
        self.events.append("bridge.start")
        return self.start_result

    def status(self) -> int:
        self.events.append("bridge.status")
        return 0

    def stop(self) -> int:
        self.events.append("bridge.stop")
        return 0


def test_parse_env_file_accepts_export_and_does_not_expand_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        '# comment\nTOKEN=secret-value\nexport URL="http://localhost:8123"\n'
        "LITERAL=$NOT_EXPANDED\n",
        encoding="utf-8",
    )

    assert parse_env_file(env_file) == {
        "TOKEN": "secret-value",
        "URL": "http://localhost:8123",
        "LITERAL": "$NOT_EXPANDED",
    }


def test_parse_env_file_rejects_malformed_lines_without_echoing_value(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("BROKEN LINE=contains-secret\n", encoding="utf-8")

    with pytest.raises(LabRunnerError, match="line 1") as error:
        parse_env_file(env_file)

    assert "contains-secret" not in str(error.value)


def test_up_selects_existing_profiles_and_passes_env_only_to_child(tmp_path: Path) -> None:
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("DOMOAI_HOME_ASSISTANT_TOKEN=secret-value\n", encoding="utf-8")
    calls: list[tuple[list[str], Mapping[str, str]]] = []

    runner = LabRunner(
        LabConfig(
            repo_root=tmp_path,
            compose_file=compose_file,
            env_file=env_file,
            docker_executable="docker",
            project_name="test-lab",
        ),
        execute=lambda argv, env: calls.append((list(argv), env)) or 0,
    )

    assert runner.up(("homeassistant", "mqtt")) == 0
    assert calls[0][0] == [
        "docker",
        "compose",
        "-p",
        "test-lab",
        "-f",
        str(compose_file),
        "--profile",
        "homeassistant",
        "up",
        "-d",
        "--build",
        "homeassistant",
        "mqtt",
    ]
    assert calls[0][1]["DOMOAI_HOME_ASSISTANT_TOKEN"] == "secret-value"


def test_process_environment_overrides_local_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DOMOAI_KNX_GATEWAY_HOST=old-wsl-address\n", encoding="utf-8")
    monkeypatch.setenv("DOMOAI_KNX_GATEWAY_HOST", "127.0.0.1")
    runner = LabRunner(
        LabConfig(repo_root=tmp_path, compose_file=tmp_path / "compose.yaml", env_file=env_file)
    )

    environment = runner._environment(include_local_env=True)

    assert environment["DOMOAI_KNX_GATEWAY_HOST"] == "127.0.0.1"


def test_unknown_service_is_rejected_before_process_launch(tmp_path: Path) -> None:
    calls: list[Sequence[str]] = []
    runner = LabRunner(
        LabConfig(repo_root=tmp_path, compose_file=tmp_path / "compose.yaml"),
        execute=lambda argv, env: calls.append(argv) or 0,
    )

    with pytest.raises(LabRunnerError, match="unknown lab service"):
        runner.up(("unknown",))

    assert calls == []


def test_down_volumes_is_explicit(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    runner = LabRunner(
        LabConfig(repo_root=tmp_path, compose_file=compose_file),
        execute=lambda argv, env: calls.append(list(argv)) or 0,
    )

    assert runner.down(remove_volumes=True) == 0
    assert calls[0][-2:] == ["down", "--volumes"]


def test_deterministic_smoke_removes_live_configuration(tmp_path: Path) -> None:
    calls: list[tuple[list[str], Mapping[str, str]]] = []
    runner = LabRunner(
        LabConfig(
            repo_root=tmp_path,
            compose_file=tmp_path / "compose.yaml",
            python_executable="python",
        ),
        execute=lambda argv, env: calls.append((list(argv), env)) or 0,
    )

    assert runner.smoke() == 0
    argv, env = calls[0]
    assert argv[:3] == ["python", "-m", "pytest"]
    assert "tests/integration/test_matter_server_fixture.py" in argv
    assert "tests/integration/test_knx_fixture.py" in argv
    assert not any(key.startswith("DOMOAI_") for key in env)


def test_default_up_services_are_only_core_local_services() -> None:
    assert DEFAULT_UP_SERVICES == ("mqtt", "zigbee2mqtt", "modbus")


def test_knx_gateway_is_available_as_an_explicit_lab_service() -> None:
    assert "knx-gateway" in SERVICE_NAMES


def test_windows_docker_gets_a_repo_relative_compose_path(tmp_path: Path) -> None:
    compose_file = tmp_path / "dev" / "lab" / "compose.yaml"
    compose_file.parent.mkdir(parents=True)
    compose_file.write_text("services: {}\n", encoding="utf-8")
    calls: list[list[str]] = []
    runner = LabRunner(
        LabConfig(
            repo_root=tmp_path,
            compose_file=compose_file,
            docker_executable="docker.exe",
        ),
        execute=lambda argv, env: calls.append(list(argv)) or 0,
    )

    assert runner.status() == 0
    assert calls[0][5] == "dev/lab/compose.yaml"


def test_up_starts_compose_before_the_external_knx_bridge(tmp_path: Path) -> None:
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    events: list[str] = []

    def execute(argv: Sequence[str], _env: Mapping[str, str]) -> int:
        events.append("compose:" + " ".join(argv[-4:]))
        return 0

    runner = LabRunner(
        LabConfig(repo_root=tmp_path, compose_file=compose_file),
        execute=execute,
        bridge_supervisor=FakeBridgeSupervisor(events),
    )

    assert runner.up(("mqtt", "battery", "knx-bridge")) == 0
    assert events[0].startswith("compose:")
    assert events[-1] == "bridge.start"


def test_down_stops_external_knx_bridge_before_compose(tmp_path: Path) -> None:
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    events: list[str] = []

    def execute(_argv: Sequence[str], _env: Mapping[str, str]) -> int:
        events.append("compose.down")
        return 0

    runner = LabRunner(
        LabConfig(repo_root=tmp_path, compose_file=compose_file),
        execute=execute,
        bridge_supervisor=FakeBridgeSupervisor(events),
    )

    assert runner.down() == 0
    assert events == ["bridge.stop", "compose.down"]


def test_bridge_service_can_be_statused_without_launching_compose(tmp_path: Path) -> None:
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    events: list[str] = []
    calls: list[Sequence[str]] = []
    runner = LabRunner(
        LabConfig(repo_root=tmp_path, compose_file=compose_file),
        execute=lambda argv, _env: calls.append(argv) or 0,
        bridge_supervisor=FakeBridgeSupervisor(events),
    )

    assert runner.status(("knx-bridge",)) == 0
    assert events == ["bridge.status"]
    assert calls == []
