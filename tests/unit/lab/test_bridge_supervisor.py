from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from domoai.lab.bridge_supervisor import (
    BridgeState,
    BridgeStatus,
    BridgeStatusStore,
    BridgeSupervisor,
    BridgeSupervisorConfig,
    mapping_digest,
)


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid


def _config(tmp_path: Path, *, knx_host: str | None = "172.26.80.1") -> BridgeSupervisorConfig:
    script = tmp_path / "knx_bridge.py"
    mapping = tmp_path / "mapping.json"
    script.write_text("# bridge", encoding="utf-8")
    mapping.write_text('{"entities": []}', encoding="utf-8")
    return BridgeSupervisorConfig(
        repo_root=tmp_path,
        python_executable="python",
        bridge_script=script,
        mapping_path=mapping,
        state_dir=tmp_path / ".lab-state",
        knx_host=knx_host,
        verify_knx_readback=False,
        readiness_timeout_seconds=0.1,
    )


def _status(pid: int | None, state: BridgeState) -> BridgeStatus:
    return BridgeStatus(
        state=state,
        pid=pid,
        started_at="2026-08-30T10:00:00Z",
        updated_at="2026-08-30T10:00:01Z",
        knx_readback_at="2026-08-30T10:00:01Z" if state is BridgeState.READY else None,
        knx_readback_ok=True if state is BridgeState.READY else None,
        mapping_path="mapping.json",
        mapping_digest="sha256:test",
        knx_host="172.26.80.1",
        knx_port=3672,
    )


def test_status_store_round_trips_and_rejects_malformed_json(tmp_path: Path) -> None:
    store = BridgeStatusStore(tmp_path / "status.json")
    store.write(_status(123, BridgeState.READY))

    loaded = store.read()
    assert loaded is not None
    assert loaded.state is BridgeState.READY
    assert loaded.pid == 123
    assert (
        json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))["schema_version"]
        == "v1"
    )

    (tmp_path / "status.json").write_text("{broken", encoding="utf-8")
    assert store.read() is None


def test_legacy_ready_status_is_loaded_for_revalidation(tmp_path: Path) -> None:
    store = BridgeStatusStore(tmp_path / "status.json")
    payload = _status(123, BridgeState.READY).to_dict()
    payload["knx_readback_ok"] = None
    payload["knx_readback_at"] = None
    store.path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = store.read()
    assert loaded is not None
    assert loaded.state is BridgeState.READY
    assert loaded.knx_readback_ok is None


def test_start_rejects_missing_knx_host_before_spawning(tmp_path: Path, capsys) -> None:
    spawned: list[Sequence[str]] = []
    supervisor = BridgeSupervisor(
        _config(tmp_path, knx_host=None),
        process_factory=lambda argv, _env, _log: spawned.append(argv) or FakeProcess(123),
    )

    assert supervisor.start() == 2
    assert spawned == []
    assert "configuration_invalid" in capsys.readouterr().out


def test_ready_status_is_not_authoritative_when_pid_is_dead(tmp_path: Path, capsys) -> None:
    config = _config(tmp_path)
    store = BridgeStatusStore(config.status_path)
    store.write(_status(456, BridgeState.READY))
    supervisor = BridgeSupervisor(config, process_probe=lambda _pid: False)

    assert supervisor.status() == 1
    assert "stale_process" in capsys.readouterr().out
    persisted = store.read()
    assert persisted is not None
    assert persisted.state is BridgeState.FAILED


def test_start_does_not_spawn_duplicate_live_bridge(tmp_path: Path, capsys) -> None:
    config = _config(tmp_path)
    existing = _status(789, BridgeState.READY)
    BridgeStatusStore(config.status_path).write(
        BridgeStatus(
            **{
                **existing.to_dict(),
                "state": BridgeState.READY,
                "mapping_digest": mapping_digest(config.resolved_mapping_path),
                "mapping_path": "mapping.json",
            }
        )
    )
    spawned: list[Sequence[str]] = []
    supervisor = BridgeSupervisor(
        config,
        process_factory=lambda argv, _env, _log: spawned.append(argv) or FakeProcess(999),
        process_probe=lambda pid: pid == 789,
    )

    assert supervisor.start() == 0
    assert spawned == []
    assert "pid=789" in capsys.readouterr().out


def test_start_rejects_live_pid_with_different_endpoint_configuration(
    tmp_path: Path, capsys
) -> None:
    config = _config(tmp_path, knx_host="172.26.80.1")
    BridgeStatusStore(config.status_path).write(_status(790, BridgeState.READY))
    current = BridgeStatusStore(config.status_path).read()
    assert current is not None
    BridgeStatusStore(config.status_path).write(
        BridgeStatus(
            **{
                **current.to_dict(),
                "state": BridgeState.READY,
                "knx_host": "172.26.80.2",
            }
        )
    )
    spawned: list[Sequence[str]] = []
    supervisor = BridgeSupervisor(
        config,
        process_factory=lambda argv, _env, _log: spawned.append(argv) or FakeProcess(999),
        process_probe=lambda pid: pid == 790,
    )

    assert supervisor.start() == 2
    assert spawned == []
    assert "configuration_conflict" in capsys.readouterr().out


def test_start_passes_explicit_endpoint_and_status_file_and_waits_for_ready(
    tmp_path: Path, capsys
) -> None:
    config = _config(tmp_path)
    spawned: list[tuple[Sequence[str], Mapping[str, str], Path]] = []
    process = FakeProcess(321)

    def spawn(argv: Sequence[str], env: Mapping[str, str], log_path: Path) -> FakeProcess:
        spawned.append((argv, env, log_path))
        BridgeStatusStore(config.status_path).write(_status(process.pid, BridgeState.READY))
        return process

    supervisor = BridgeSupervisor(
        config, process_factory=spawn, process_probe=lambda pid: pid == 321
    )

    assert supervisor.start() == 0
    argv, env, log_path = spawned[0]
    assert "--knx-host" in argv and "172.26.80.1" in argv
    assert "--knx-port" in argv and "3672" in argv
    assert "--status-file" in argv and str(config.status_path) in argv
    assert env["PYTHONUNBUFFERED"] == "1"
    assert log_path == config.log_path
    assert "status=ready" in capsys.readouterr().out


def test_stop_signals_only_recorded_owned_pid_and_persists_stopped(tmp_path: Path, capsys) -> None:
    config = _config(tmp_path)
    BridgeStatusStore(config.status_path).write(_status(654, BridgeState.READY))
    signals: list[tuple[int, int]] = []
    alive = True

    def probe(pid: int) -> bool:
        return pid == 654 and alive

    def send(pid: int, signal: int) -> None:
        nonlocal alive
        signals.append((pid, signal))
        alive = False

    supervisor = BridgeSupervisor(config, process_probe=probe, signal_process=send)

    assert supervisor.stop() == 0
    assert len(signals) == 1
    assert signals[0][0] == 654
    assert BridgeStatusStore(config.status_path).read().state is BridgeState.STOPPED
    assert "status=stopped" in capsys.readouterr().out


def test_degraded_bridge_status_is_non_ready(tmp_path: Path, capsys) -> None:
    config = _config(tmp_path)
    BridgeStatusStore(config.status_path).write(_status(111, BridgeState.DEGRADED))
    supervisor = BridgeSupervisor(config, process_probe=lambda _pid: True)

    assert supervisor.status() == 1
    assert "status=degraded" in capsys.readouterr().out


def test_start_downgrades_bridge_when_knx_readback_probe_fails(tmp_path: Path, capsys) -> None:
    base = _config(tmp_path)
    config = replace(base, verify_knx_readback=True)
    process = FakeProcess(222)

    def spawn(_argv: Sequence[str], _env: Mapping[str, str], _log: Path) -> FakeProcess:
        BridgeStatusStore(config.status_path).write(_status(process.pid, BridgeState.READY))
        return process

    supervisor = BridgeSupervisor(
        config,
        process_factory=spawn,
        process_probe=lambda pid: pid == process.pid,
        readiness_probe=lambda: False,
    )

    assert supervisor.start() == 1
    output = capsys.readouterr().out
    assert "status=degraded" in output
    assert "knx_readback_unavailable" in output
    status = BridgeStatusStore(config.status_path).read()
    assert status is not None
    assert status.state is BridgeState.DEGRADED
    assert status.knx_readback_ok is False


def test_successful_knx_readback_promotes_bridge_to_ready(tmp_path: Path, capsys) -> None:
    base = _config(tmp_path)
    config = replace(base, verify_knx_readback=True)
    process = FakeProcess(223)

    def spawn(_argv: Sequence[str], _env: Mapping[str, str], _log: Path) -> FakeProcess:
        BridgeStatusStore(config.status_path).write(
            BridgeStatus(
                state=BridgeState.DEGRADED,
                pid=process.pid,
                started_at="2026-08-30T10:00:00Z",
                updated_at="2026-08-30T10:00:01Z",
                last_state_at="2026-08-30T10:00:01Z",
                mapping_path="mapping.json",
                mapping_digest="sha256:test",
                knx_host="172.26.80.1",
                knx_port=3672,
            )
        )
        return process

    supervisor = BridgeSupervisor(
        config,
        process_factory=spawn,
        process_probe=lambda pid: pid == process.pid,
        readiness_probe=lambda: True,
    )

    assert supervisor.start() == 0
    output = capsys.readouterr().out
    assert "status=ready" in output
    status = BridgeStatusStore(config.status_path).read()
    assert status is not None
    assert status.state is BridgeState.READY
    assert status.knx_readback_ok is True


def test_environment_keeps_runtime_knx_mapping_separate_from_battery_bridge() -> None:
    config = BridgeSupervisorConfig.from_environment(
        Path("/workspace"),
        {
            "DOMOAI_KNX_CONFIG_PATH": "dev/lab/configs/knx-virtual.json",
            "DOMOAI_KNX_BRIDGE_MAPPING_PATH": "dev/lab/configs/knx-battery-virtual.json",
            "DOMOAI_KNX_GATEWAY_HOST": "172.26.80.1",
        },
        python_executable="python",
    )

    assert config.mapping_path == Path("dev/lab/configs/knx-battery-virtual.json")
