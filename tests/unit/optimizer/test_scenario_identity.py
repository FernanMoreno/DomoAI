from __future__ import annotations

from datetime import UTC, datetime, timedelta

from domoai.optimizer.scenario import OptimizationScenario, scenario_definition_digest


def _scenario_payload() -> dict[str, object]:
    start = datetime(2026, 9, 4, 12, tzinfo=UTC)
    return {
        "id": "identity-scenario",
        "horizon": {
            "start": start.isoformat(),
            "end": (start + timedelta(minutes=30)).isoformat(),
            "resolution_minutes": 15,
            "timezone": "Europe/Madrid",
        },
        "conservative": True,
        "solver_time_limit_seconds": 2.0,
    }


def test_equivalent_normalized_scenarios_have_the_same_digest() -> None:
    first = OptimizationScenario.model_validate(_scenario_payload())
    second = OptimizationScenario.model_validate(
        {
            "solver_time_limit_seconds": 2.0,
            "conservative": True,
            "horizon": first.horizon.model_dump(mode="json"),
            "id": "identity-scenario",
        }
    )

    assert scenario_definition_digest(first) == scenario_definition_digest(second)


def test_semantic_scenario_change_has_a_different_digest() -> None:
    first = OptimizationScenario.model_validate(_scenario_payload())
    changed = first.model_copy(update={"conservative": False})

    assert scenario_definition_digest(first) != scenario_definition_digest(changed)


def test_digest_is_versioned_and_uses_canonical_json() -> None:
    scenario = OptimizationScenario.model_validate(_scenario_payload())

    digest = scenario_definition_digest(scenario)

    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64
    assert scenario.schema_version == "v1"
