"""Composition checks for the runtime/application architecture boundary."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "domoai"


def _package_edges() -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for path in SRC.rglob("*.py"):
        source = "domoai." + ".".join(path.relative_to(SRC).with_suffix("").parts)
        if source.endswith(".__init__"):
            source = source[:-9]
        if source == "domoai":
            continue
        source_package = source.split(".")[1]
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                targets = [node.module]
            for target in targets:
                if not target.startswith("domoai."):
                    continue
                target_package = target.split(".")[1]
                if source_package != target_package:
                    edges.add((source_package, target_package))
    return edges


def test_runtime_has_no_reverse_orchestration_edges() -> None:
    forbidden = {
        ("runtime", "application"),
        ("runtime", "adapters"),
        ("runtime", "persistence"),
        ("runtime", "config"),
        ("config", "optimizer"),
    }
    assert not (_package_edges() & forbidden)


def test_complete_import_linter_policy_is_green() -> None:
    result = subprocess.run(
        ["uv", "run", "lint-imports"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_application_owns_orchestration_modules() -> None:
    from domoai.application.bundle_commit import BundleCommitService
    from domoai.application.event_consumer import RuntimeEventConsumer
    from domoai.application.executor import PlanExecutor
    from domoai.application.metrics import RuntimeMetricsCollector
    from domoai.application.policy_engine import PolicyEngine
    from domoai.application.recovery import PlanRecoveryService
    from domoai.application.scheduler import Scheduler

    assert all(
        component.__module__.startswith("domoai.application.")
        for component in (
            BundleCommitService,
            RuntimeEventConsumer,
            PlanExecutor,
            RuntimeMetricsCollector,
            PolicyEngine,
            PlanRecoveryService,
            Scheduler,
        )
    )
