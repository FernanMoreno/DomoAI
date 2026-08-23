"""Small source-derived architecture gate for invariants Import Linter owns."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "domoai"


def _module_for(path: Path) -> str:
    return "domoai." + ".".join(path.relative_to(SRC).with_suffix("").parts)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names if alias.name.startswith("domoai."))
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("domoai."):
            result.add(node.module)
    return result


def main() -> None:
    violations: list[str] = []
    for path in SRC.rglob("*.py"):
        source = _module_for(path)
        for target in _imports(path):
            if source == "domoai.domain" or source.startswith("domoai.domain."):
                if not (target == "domoai.domain" or target.startswith("domoai.domain.")):
                    violations.append(f"domain import: {source} -> {target}")
            if source.startswith("domoai.adapters.") and target.startswith("domoai.adapters."):
                source_adapter = source.split(".")[2]
                target_adapter = target.split(".")[2]
                shared = {"sdk", "fixtures"}
                if (
                    source_adapter != target_adapter
                    and source_adapter not in shared
                    and target_adapter not in shared
                ):
                    violations.append(f"adapter sibling import: {source} -> {target}")

    importlinter = ROOT / ".importlinter"
    if not importlinter.is_file():
        violations.append("missing .importlinter architecture contract")
    else:
        text = importlinter.read_text(encoding="utf-8")
        for marker in ("acyclic_siblings", "type = independence", "type = layers"):
            if marker not in text:
                violations.append(f"architecture contract missing: {marker}")

    if violations:
        raise SystemExit("architecture contract violations:\n- " + "\n- ".join(violations))
    print("architecture contracts kept: domain core, adapter independence, Import Linter policy")


if __name__ == "__main__":
    main()
