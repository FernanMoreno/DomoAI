from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


def _docker_available() -> bool:
    try:
        result = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


pytestmark = pytest.mark.skipif(
    not _docker_available(), reason="Docker daemon not reachable; skipping real MQTT composition"
)


def _wait_for_port(host: str, port: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.25)
    raise AssertionError(f"MQTT port did not open: {host}:{port}")


@pytest.mark.composition
def test_local_broker_rejects_anonymous_and_accepts_configured_credentials(tmp_path: Path) -> None:
    del tmp_path
    username = "domoai-test"
    password = f"ops006-test-{os.getpid()}"
    invalid_password = f"{password}-invalid"
    project = f"domoai-ops006-{os.getpid()}"
    environment = os.environ | {
        "DOMOAI_MQTT_USERNAME": username,
        "DOMOAI_MQTT_PASSWORD": password,
        "DOMOAI_MQTT_HOST_PORT": "28883",
    }
    compose = ["docker", "compose", "-p", project, "-f", "dev/lab/compose.yaml"]
    try:
        up = subprocess.run(
            [*compose, "up", "-d", "mqtt"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert up.returncode == 0, up.stderr
        _wait_for_port("127.0.0.1", 28883)

        authenticated = subprocess.run(
            [
                *compose,
                "exec",
                "-T",
                "mqtt",
                "mosquitto_pub",
                "-h",
                "127.0.0.1",
                "-u",
                username,
                "-P",
                password,
                "-t",
                "domoai/ops006/authenticated",
                "-m",
                "ok",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert authenticated.returncode == 0, authenticated.stderr

        invalid_credentials = subprocess.run(
            [
                *compose,
                "exec",
                "-T",
                "mqtt",
                "mosquitto_pub",
                "-h",
                "127.0.0.1",
                "-u",
                username,
                "-P",
                invalid_password,
                "-t",
                "domoai/ops006/invalid-credentials",
                "-m",
                "must-fail",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert invalid_credentials.returncode != 0

        anonymous = subprocess.run(
            [
                *compose,
                "exec",
                "-T",
                "mqtt",
                "mosquitto_pub",
                "-h",
                "127.0.0.1",
                "-t",
                "domoai/ops006/anonymous",
                "-m",
                "must-fail",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert anonymous.returncode != 0
    finally:
        subprocess.run(
            [*compose, "down", "--volumes"],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


@pytest.mark.composition
def test_base_compose_fails_closed_without_mqtt_credentials() -> None:
    environment = os.environ.copy()
    environment.pop("DOMOAI_MQTT_USERNAME", None)
    environment.pop("DOMOAI_MQTT_PASSWORD", None)
    compose = ["docker", "compose", "-f", "dev/lab/compose.yaml", "config"]
    rendered = subprocess.run(
        compose,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rendered.returncode != 0
    assert "DOMOAI_MQTT_USERNAME" in rendered.stderr or "DOMOAI_MQTT_PASSWORD" in rendered.stderr


@pytest.mark.composition
def test_shared_overlay_fails_closed_without_each_tls_input(tmp_path: Path) -> None:
    environment = os.environ | {
        "DOMOAI_MQTT_USERNAME": "domoai-test",
        "DOMOAI_MQTT_PASSWORD": f"ops006-test-{os.getpid()}",
        "DOMOAI_MQTT_TLS_BIND": "127.0.0.1",
        "DOMOAI_MQTT_TLS_CERT_FILE": str(tmp_path / "server.crt"),
        "DOMOAI_MQTT_TLS_KEY_FILE": str(tmp_path / "server.key"),
    }
    compose = [
        "docker",
        "compose",
        "-f",
        "dev/lab/compose.yaml",
        "-f",
        "dev/lab/compose.shared-mqtt-tls.yaml",
        "config",
    ]
    for variable in (
        "DOMOAI_MQTT_TLS_BIND",
        "DOMOAI_MQTT_TLS_CERT_FILE",
        "DOMOAI_MQTT_TLS_KEY_FILE",
    ):
        missing = environment.copy()
        missing.pop(variable)
        rendered = subprocess.run(
            compose,
            cwd=ROOT,
            env=missing,
            capture_output=True,
            text=True,
            check=False,
        )
        assert rendered.returncode != 0, variable
        assert variable in rendered.stderr, rendered.stderr


@pytest.mark.composition
def test_shared_overlay_accepts_authenticated_tls_and_has_no_remote_plaintext(
    tmp_path: Path,
) -> None:
    username = "domoai-test"
    password = f"ops006-test-{os.getpid()}"
    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    certificate = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert certificate.returncode == 0, certificate.stderr

    project = f"domoai-ops006-tls-{os.getpid()}"
    environment = os.environ | {
        "DOMOAI_MQTT_USERNAME": username,
        "DOMOAI_MQTT_PASSWORD": password,
        "DOMOAI_MQTT_HOST_PORT": "28885",
        "DOMOAI_MQTT_TLS_BIND": "127.0.0.1",
        "DOMOAI_MQTT_TLS_HOST_PORT": "28886",
        "DOMOAI_MQTT_TLS_CERT_FILE": str(cert),
        "DOMOAI_MQTT_TLS_KEY_FILE": str(key),
    }
    compose = [
        "docker",
        "compose",
        "-p",
        project,
        "-f",
        "dev/lab/compose.yaml",
        "-f",
        "dev/lab/compose.shared-mqtt-tls.yaml",
    ]
    try:
        up = subprocess.run(
            [*compose, "up", "-d", "mqtt"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert up.returncode == 0, up.stderr
        _wait_for_port("127.0.0.1", 28886)

        tls_publish = subprocess.run(
            [
                *compose,
                "exec",
                "-T",
                "mqtt",
                "mosquitto_pub",
                "-h",
                "127.0.0.1",
                "-p",
                "8883",
                "--cafile",
                "/mosquitto/certs/server.crt",
                "-u",
                username,
                "-P",
                password,
                "-t",
                "domoai/ops006/tls",
                "-m",
                "ok",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert tls_publish.returncode == 0, tls_publish.stderr

        tls_anonymous = subprocess.run(
            [
                *compose,
                "exec",
                "-T",
                "mqtt",
                "mosquitto_pub",
                "-h",
                "127.0.0.1",
                "-p",
                "8883",
                "--cafile",
                "/mosquitto/certs/server.crt",
                "-t",
                "domoai/ops006/tls-anonymous",
                "-m",
                "must-fail",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert tls_anonymous.returncode != 0
    finally:
        subprocess.run(
            [*compose, "down", "--volumes"],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
