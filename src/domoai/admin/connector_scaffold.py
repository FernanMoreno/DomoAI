"""Scaffold and conformance checks for third-party connection providers."""

from __future__ import annotations

import importlib
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from domoai.admin.connector_conformance import validate_provider
from domoai.runtime.connection_provider_base import ConnectionProviderIncompleteError

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_PYTHON_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


@dataclass(frozen=True)
class ConnectorScaffoldRequest:
    connector_id: str
    display_name: str
    protocol: str
    output: Path


@dataclass(frozen=True)
class ConnectorDiagnostic:
    code: str
    message: str


@dataclass(frozen=True)
class ConnectorScaffoldResult:
    status: str
    output: str
    connector_id: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class ConnectorCheckResult:
    status: str
    connector_id: str | None
    diagnostics: tuple[ConnectorDiagnostic, ...]


def create_connector_scaffold(request: ConnectorScaffoldRequest) -> ConnectorScaffoldResult:
    _validate_request(request)
    output = request.output
    if output.exists() and (output.is_symlink() or not output.is_dir() or any(output.iterdir())):
        raise ValueError("connector scaffold output must be a new empty directory")
    output.mkdir(parents=True, exist_ok=True)
    package = request.connector_id.replace("-", "_").replace(".", "_")
    if not _PYTHON_IDENTIFIER.fullmatch(package):
        raise ValueError("connector ID cannot be converted into a Python package name")

    files = {
        "pyproject.toml": _pyproject(request, package),
        "README.md": _readme(request),
        f"src/{package}/__init__.py": _init(package),
        f"src/{package}/provider.py": _provider(request, package),
        f"src/{package}/adapter.py": _adapter(package),
        "tests/test_provider_contract.py": _provider_test(package),
    }
    for relative, content in files.items():
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return ConnectorScaffoldResult(
        status="created",
        output=str(output),
        connector_id=request.connector_id,
        files=tuple(files),
    )


def check_connector_scaffold(root: Path) -> ConnectorCheckResult:
    try:
        if root.is_symlink() or not root.is_dir():
            return _failed(
                "provider_scaffold_invalid", "provider scaffold root must be a directory"
            )
        project = _read_project(root)
        entry_points = project["project"]["entry-points"]["domoai.connection_providers"]
        if not isinstance(entry_points, dict) or len(entry_points) != 1:
            return _failed(
                "provider_entry_point_invalid",
                "exactly one provider entry point is required",
            )
        entry_name, entry_point = next(iter(entry_points.items()))
        module_name, separator, factory_name = str(entry_point).partition(":")
        if not separator:
            return _failed("provider_entry_point_invalid", "entry point must name module:function")
        src = root / "src"
        if src.is_symlink() or not src.is_dir():
            return _failed(
                "provider_scaffold_invalid", "provider scaffold source directory is required"
            )
        package_name = module_name.split(".", 1)[0]
        adapter_file = src / package_name / "adapter.py"
        if adapter_file.is_symlink() or not adapter_file.is_file():
            return _failed(
                "provider_scaffold_invalid", "provider adapter extension point is required"
            )
        previous_modules = {
            name: module
            for name, module in sys.modules.items()
            if name == package_name or name.startswith(f"{package_name}.")
        }
        for name in previous_modules:
            sys.modules.pop(name, None)
        sys.path.insert(0, str(src))
        try:
            provider_module = importlib.import_module(module_name)
            factory = getattr(provider_module, factory_name)
            provider = factory()
            conformance = validate_provider(provider)
            if conformance:
                return ConnectorCheckResult(
                    status="failed",
                    connector_id=None,
                    diagnostics=tuple(
                        ConnectorDiagnostic(item.code, item.message) for item in conformance
                    ),
                )
            connector_id = provider.manifest.connector_id
            if entry_name != connector_id:
                return _failed(
                    "provider_entry_point_invalid",
                    "entry point name must match the provider connector ID",
                )
            if (
                getattr(provider_module, "PROVIDER_IMPLEMENTATION_STATUS", "complete")
                == "incomplete"
            ):
                return ConnectorCheckResult(
                    status="incomplete",
                    connector_id=connector_id,
                    diagnostics=(
                        ConnectorDiagnostic(
                            code=ConnectionProviderIncompleteError.code,
                            message="provider adapter implementation is still required",
                        ),
                    ),
                )
            return ConnectorCheckResult(status="ready", connector_id=connector_id, diagnostics=())
        finally:
            sys.path.remove(str(src))
            for name in list(sys.modules):
                if name == package_name or name.startswith(f"{package_name}."):
                    sys.modules.pop(name, None)
            sys.modules.update(previous_modules)
    except Exception:  # noqa: BLE001 - the check boundary returns only sanitized diagnostics
        return _failed(
            "provider_entry_point_invalid", "provider scaffold does not conform to the SDK"
        )


def _validate_request(request: ConnectorScaffoldRequest) -> None:
    for field_name, value in (
        ("connector ID", request.connector_id),
        ("display name", request.display_name),
        ("protocol", request.protocol),
    ):
        if not value.strip():
            raise ValueError(f"{field_name} must not be blank")
        if len(value) > (64 if field_name == "protocol" else 128) or any(
            character in value for character in ('"', "\\", "\r", "\n")
        ):
            raise ValueError(f"{field_name} contains unsupported template characters")
    if not _IDENTIFIER.fullmatch(request.connector_id):
        raise ValueError(
            "connector ID must use lowercase letters, digits, dots, dashes or underscores"
        )
    if any(
        marker in value.casefold()
        for value in (request.display_name, request.protocol)
        for marker in ("token", "password", "secret", "credential", "authorization")
    ):
        raise ValueError("provider metadata must not contain sensitive material")


def _read_project(root: Path) -> dict[str, Any]:
    path = root / "pyproject.toml"
    with path.open("rb") as stream:
        return tomllib.load(stream)


def _failed(code: str, message: str) -> ConnectorCheckResult:
    return ConnectorCheckResult(
        status="failed", connector_id=None, diagnostics=(ConnectorDiagnostic(code, message),)
    )


def _pyproject(request: ConnectorScaffoldRequest, package: str) -> str:
    return f'''[build-system]
requires = ["hatchling>=1.27,<2"]
build-backend = "hatchling.build"

[project]
name = "domoai-connector-{request.connector_id.replace("_", "-")}"
version = "0.1.0"
description = "DomoAI connector for {request.display_name}"
requires-python = ">=3.12,<3.13"
dependencies = ["domoai>=0.1.0"]

[project.entry-points."domoai.connection_providers"]
"{request.connector_id}" = "{package}.provider:create_provider"

[tool.hatch.build.targets.wheel]
packages = ["src/{package}"]
'''


def _readme(request: ConnectorScaffoldRequest) -> str:
    return f'''# {request.display_name} DomoAI connector

Generated with `domoai-admin connector create`.

The scaffold implements the five provider entry points and deliberately reports
`provider_adapter_not_implemented` until the protocol adapter is completed.

Run:

```bash
uv run domoai-admin connector check .
uv run pytest
```
'''


def _init(package: str) -> str:
    class_name = f"{package.title().replace('_', '')}ConnectionProvider"
    return f'''from .provider import {class_name}, create_provider

__all__ = ["{class_name}", "create_provider"]
'''


def _provider(request: ConnectorScaffoldRequest, package: str) -> str:
    class_name = f"{package.title().replace('_', '')}ConnectionProvider"
    return f'''"""Generated DomoAI connection provider scaffold."""

from domoai.domain.connections import ConnectionCandidate, ProbeResult, ValidationResult
from domoai.runtime.connection_ports import ConnectionProviderManifest, ConnectionSetup
from domoai.runtime.connection_provider_base import ConnectionProviderBase
from domoai.runtime.ports import AdapterPort

PROVIDER_IMPLEMENTATION_STATUS = "incomplete"


class {class_name}(ConnectionProviderBase):
    manifest = ConnectionProviderManifest(
        connector_id="{request.connector_id}",
        display_name="{request.display_name}",
        protocol="{request.protocol}",
        version="0.1.0",
    )

    async def discover(self) -> list[ConnectionCandidate]:
        return await super().discover()

    async def probe(self, candidate: ConnectionCandidate) -> ProbeResult:
        return await super().probe(candidate)

    async def validate(self, setup: ConnectionSetup) -> ValidationResult:
        return await super().validate(setup)

    async def build_adapter(self, setup: ConnectionSetup, *, adapter_id: str) -> AdapterPort:
        return await super().build_adapter(setup, adapter_id=adapter_id)

    async def diagnose(self, setup: ConnectionSetup):
        return await super().diagnose(setup)


def create_provider() -> {class_name}:
    return {class_name}()
'''


def _adapter(package: str) -> str:
    return '''"""Protocol adapter implementation goes here."""

# Keep protocol-specific dependencies in this module. The provider boundary
# must still return the runtime's AdapterPort.
'''


def _provider_test(package: str) -> str:
    return f'''from {package}.provider import create_provider


def test_provider_factory_exposes_a_manifest():
    provider = create_provider()
    assert provider.manifest.connector_id
    assert provider.manifest.protocol
'''


__all__ = [
    "ConnectorCheckResult",
    "ConnectorDiagnostic",
    "ConnectorScaffoldRequest",
    "ConnectorScaffoldResult",
    "check_connector_scaffold",
    "create_connector_scaffold",
]
