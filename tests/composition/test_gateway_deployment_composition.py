from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_complete_stack_has_one_gateway_and_external_knx_boundary() -> None:
    compose = (ROOT / "deploy/compose.yaml").read_text(encoding="utf-8")
    environment = (ROOT / "deploy/gateway.env.example").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert "  gateway:" in compose
    assert 'command: ["domoai-mcp-gateway"]' in compose
    assert "homeassistant" in compose
    assert "mqtt" in compose
    assert "8124:8124" in compose
    assert "DOMOAI_MCP_PORT=8124" in environment
    assert "DOMOAI_KNX_GATEWAY_HOST=host.docker.internal" in environment
    assert "DOMOAI_KNX_GATEWAY_PORT=3672" in environment
    assert "DOMOAI_KNX_GATEWAY_ROUTE_BACK=0" in environment
    assert "DOMOAI_KNX_GATEWAY_PORT=3671" not in environment
    assert "dev/lab/" in dockerignore


def test_deployment_does_not_copy_lab_or_runtime_secrets_into_image() -> None:
    dockerfile = (ROOT / "deploy/Dockerfile").read_text(encoding="utf-8")

    assert "COPY src ./src" in dockerfile
    assert "COPY dev/lab" not in dockerfile
    assert "COPY deploy/gateway.env" not in dockerfile
