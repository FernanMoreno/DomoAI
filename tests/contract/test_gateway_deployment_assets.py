from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_gateway_deployment_is_available_outside_the_lab_tree() -> None:
    expected = {
        "deploy/README.md",
        "deploy/Dockerfile",
        "deploy/compose.yaml",
        "deploy/gateway.env.example",
        "deploy/wsl/run-gateway.sh",
        "deploy/windows/run-gateway.ps1",
        "deploy/reverse-proxy/Caddyfile",
        ".dockerignore",
        "docs/unified-mcp.md",
    }

    assert all((ROOT / path).is_file() for path in expected)
    assert not any(path.startswith("dev/lab/") for path in expected)


def test_container_gateway_uses_distinct_gateway_and_protocol_coordinates() -> None:
    compose = (ROOT / "deploy/compose.yaml").read_text(encoding="utf-8")
    environment = (ROOT / "deploy/gateway.env.example").read_text(encoding="utf-8")

    assert "domoai-mcp-gateway" in compose
    assert '"8124:8124"' in compose
    assert "DOMOAI_MCP_PORT=8124" in environment
    assert "DOMOAI_KNX_GATEWAY_PORT=3672" in environment
    assert "host.docker.internal" in environment


def test_container_context_excludes_lab_and_runtime_secrets() -> None:
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert "dev/lab/" in dockerignore
    assert ".env*" in dockerignore
    assert "*.sqlite3" in dockerignore
