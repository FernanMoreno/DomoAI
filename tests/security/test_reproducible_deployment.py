from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_gateway_build_uses_digest_pinned_inputs_and_locked_dependencies() -> None:
    dockerfile = (ROOT / "deploy/Dockerfile").read_text(encoding="utf-8")

    assert (
        "FROM python:3.12-slim@sha256:"
        "78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"
        in dockerfile
    )
    assert (
        "FROM ghcr.io/astral-sh/uv:0.12.4@sha256:"
        "d0a6eca6c669dc7e9c51218707b8438a3d30402733d739dcc00adb3e213e8f5c AS uv"
        in dockerfile
    )
    assert "uv export --locked --no-dev --format requirements-txt" in dockerfile
    assert "--no-emit-project" in dockerfile
    assert "uv pip install --system --require-hashes" in dockerfile
    assert "uv pip install --system --no-deps ." in dockerfile
    assert "pip install ." not in dockerfile


def test_compose_external_images_are_immutable_digest_references() -> None:
    compose = yaml.safe_load((ROOT / "deploy/compose.yaml").read_text(encoding="utf-8"))

    for service_name, service in compose["services"].items():
        image = service.get("image")
        if image is not None:
            assert "@sha256:" in image, f"{service_name} image is mutable: {image}"
