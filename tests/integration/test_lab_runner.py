from __future__ import annotations

from pathlib import Path

from domoai.lab.runner import LabConfig, LabRunner


def test_fixture_smoke_plan_is_repeatable_and_does_not_load_local_env(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DOMOAI_HOME_ASSISTANT_TOKEN=fixture-secret\n", encoding="utf-8")
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def execute(argv: tuple[str, ...], env: dict[str, str]) -> int:
        calls.append((argv, env))
        return 0

    runner = LabRunner(
        LabConfig(repo_root=tmp_path, compose_file=tmp_path / "compose.yaml", env_file=env_file),
        execute=execute,
    )

    assert runner.smoke() == 0
    first = calls[-1]
    assert runner.smoke() == 0
    second = calls[-1]

    assert first[0] == second[0]
    assert "DOMOAI_HOME_ASSISTANT_TOKEN" not in first[1]
    assert all(path.endswith(".py") for path in first[0][4:])
