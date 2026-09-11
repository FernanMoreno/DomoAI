from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_gateway_image_declares_a_dedicated_non_root_runtime_identity() -> None:
    dockerfile = (ROOT / "deploy/Dockerfile").read_text(encoding="utf-8")

    assert "addgroup --system --gid 10001 domoai" in dockerfile
    assert "adduser --system --uid 10001" in dockerfile
    assert "chown -R domoai:domoai /app" in dockerfile
    assert "USER domoai:domoai" in dockerfile


def test_gateway_service_has_least_privilege_filesystem_and_linux_security() -> None:
    compose = yaml.safe_load((ROOT / "deploy/compose.yaml").read_text(encoding="utf-8"))
    gateway = compose["services"]["gateway"]

    assert gateway["user"] == "10001:10001"
    assert gateway["read_only"] is True
    assert gateway["cap_drop"] == ["ALL"]
    assert gateway["security_opt"] == ["no-new-privileges:true"]
    assert gateway["tmpfs"] == ["/tmp:rw,noexec,nosuid,size=64m"]
    assert "domoai-data:/app/data" in gateway["volumes"]
    assert (
        "${DOMOAI_MCP_CLIENT_TOKEN_FILE_HOST:-./clients.json}:"
        "/run/secrets/mcp-clients.json:ro"
        in gateway["volumes"]
    )
    assert (
        "${DOMOAI_KNX_CONFIG_PATH_HOST:-./knx-config.json}:"
        "/app/config/knx-config.json:ro"
        in gateway["volumes"]
    )
