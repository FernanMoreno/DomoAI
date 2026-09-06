from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_host_wrappers_reference_core_without_vendor_or_direct_adapter_routes() -> None:
    for host in ("claude", "codex", "generic-mcp"):
        content = (ROOT / "skills" / host / "README.md").read_text(encoding="utf-8")
        assert "core" in content
        assert "direct adapter" not in content.lower()
        assert "vendor-specific" not in content.lower()
        assert "MCP" in content
