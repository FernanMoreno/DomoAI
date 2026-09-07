from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy" / "multihost" / "qualification" / "compose.yaml"
RUNNER = ROOT / "deploy" / "multihost" / "qualification" / "runner" / "Dockerfile"


def test_lab_topology_declares_two_host_participants_without_host_ports() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")

    assert "host-a:" in compose
    assert "host-b:" in compose
    assert "DOMOAI_INSTANCE_ID: host-a" in compose
    assert "DOMOAI_INSTANCE_ID: host-b" in compose
    assert "com.domoai.lab: \"true\"" in compose
    assert "ports:" not in compose
    assert RUNNER.is_file()


def test_host_image_contains_the_v2_agent_entrypoint() -> None:
    dockerfile = RUNNER.read_text(encoding="utf-8")

    assert "multihost_host" in dockerfile
    assert "COPY skills/core ./skills/core" in dockerfile
