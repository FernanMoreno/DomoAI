"""Typed JSON contracts for scoped declarations and provider provenance."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from domoai.optimizer.base_load import DeclaredBaseLoadProfile, DeclaredBaseLoadProvider
from domoai.optimizer.providers import EnergyProviderError
from tests.fixtures.base_load import horizon, profile_payload, write_profile
from tests.unit.optimizer.test_base_load import base_series, composer


@pytest.mark.parametrize(
    "update",
    [
        {"tenant_id": ""},
        {"household_id": ""},
        {"power": -1},
        {"power": float("inf")},
        {"power": True},
        {"unit": "W"},
        {"unit": "kWh"},
        {"kind": "forecast"},
        {"scope": "net_meter"},
        {"scope": "whole_home"},
        {"included_ev_kw": 2},
        {"declared_at": "2026-09-09T08:00:00"},
        {"valid_from": "2026-09-09T08:00:00"},
        {"valid_until": "2026-09-09T08:00:00Z"},
    ],
)
def test_profile_rejects_nonresidual_or_malformed_declarations(update) -> None:
    with pytest.raises(ValidationError):
        DeclaredBaseLoadProfile.model_validate(profile_payload(**update))


@pytest.mark.parametrize("missing", ["tenant_id", "household_id", "kind", "scope"])
def test_profile_scope_and_kind_require_explicit_declaration(missing) -> None:
    payload = profile_payload()
    payload.pop(missing)
    with pytest.raises(ValidationError):
        DeclaredBaseLoadProfile.model_validate(payload)


@pytest.mark.parametrize("scope", [("tenant-b", "home-a"), ("tenant-a", "home-b")])
def test_profile_cannot_cross_tenant_or_household(scope) -> None:
    with pytest.raises(EnergyProviderError) as failure:
        DeclaredBaseLoadProvider(
            DeclaredBaseLoadProfile.model_validate(profile_payload()),
            tenant_id=scope[0],
            household_id=scope[1],
        )
    assert failure.value.diagnostic.code == "base_load_scope_mismatch"


def test_default_household_does_not_make_other_tenant_profile_valid() -> None:
    profile = DeclaredBaseLoadProfile.model_validate(profile_payload(household_id="default"))
    with pytest.raises(EnergyProviderError):
        DeclaredBaseLoadProvider(profile, tenant_id="tenant-b", household_id="default")


def test_both_scope_ids_change_revision_without_rejuvenating_date() -> None:
    revisions = set()
    for tenant_id, household_id in [
        ("tenant-a", "home-a"),
        ("tenant-b", "home-a"),
        ("tenant-a", "home-b"),
    ]:
        profile = DeclaredBaseLoadProfile.model_validate(
            profile_payload(tenant_id=tenant_id, household_id=household_id)
        )
        provider = DeclaredBaseLoadProvider(profile, tenant_id=tenant_id, household_id=household_id)
        series = provider.get_forecast(horizon())
        revisions.add(series.source_revision)
        assert series.observed_at == profile.declared_at
        profile.power = 99  # Mutating caller-owned data must not affect a bound provider.
        assert provider.get_forecast(horizon()).points[0].power == 1
    assert len(revisions) == 3


@pytest.mark.parametrize(
    "update,code",
    [
        ({"declared_at": "2026-09-11T08:00:00Z"}, "invalid_observed_at"),
        ({"valid_from": "2026-09-10T11:00:00Z"}, "horizon_mismatch"),
        ({"valid_until": "2026-09-10T11:00:00Z"}, "horizon_mismatch"),
    ],
)
def test_invalid_reserve_time_never_falls_back_to_zero(update, code) -> None:
    profile = DeclaredBaseLoadProfile.model_validate(profile_payload(**update))
    provider = DeclaredBaseLoadProvider(profile, tenant_id="tenant-a", household_id="home-a")
    with pytest.raises(EnergyProviderError) as failure:
        composer(provider).get_context(horizon())
    assert failure.value.diagnostic.code == code


@pytest.mark.parametrize("contents", ["{bad-json", '{"secret":"private-token"}'])
def test_invalid_file_error_does_not_expose_path_or_source_data(tmp_path: Path, contents) -> None:
    path = tmp_path / "private-token.json"
    path.write_text(contents)
    with pytest.raises(EnergyProviderError) as failure:
        DeclaredBaseLoadProvider.from_file(path, tenant_id="tenant-a", household_id="home-a")
    assert failure.value.diagnostic.code == "provider_invalid"
    assert "private-token" not in str(failure.value)


def test_published_base_schema_and_energy_context_schema_are_current() -> None:
    from scripts.export_schemas import EnergyContextV1

    from domoai.optimizer.providers import BaseLoadForecastSeries

    root = Path(__file__).resolve().parents[2] / "schemas" / "v1"
    for name, model in [
        ("declared-base-load-profile", DeclaredBaseLoadProfile),
        ("base-load-forecast-series", BaseLoadForecastSeries),
        ("energy-context", EnergyContextV1),
    ]:
        assert (root / f"{name}.schema.json").exists(), f"missing public schema: {name}"
        assert json.loads((root / f"{name}.schema.json").read_text()) == model.model_json_schema()


def test_series_roundtrip_preserves_reserve_type_and_interval(tmp_path: Path) -> None:
    provider = DeclaredBaseLoadProvider.from_file(
        write_profile(tmp_path / "base.json"), tenant_id="tenant-a", household_id="home-a"
    )
    series = provider.get_forecast(horizon())
    assert type(series).model_validate_json(series.model_dump_json()) == series
    context = composer(provider).get_context(horizon())
    assert context.base_load_source.kind == "declared_reserve"
    assert context.base_load_source.valid_until == series.valid_until
    assert context.observed_at == series.observed_at
    assert base_series().scope == "uncontrolled_demand"


def test_absent_base_source_remains_readable_by_exact_pre_a09_schema() -> None:
    from jsonschema import Draft202012Validator

    from domoai.optimizer.energy import EnergyContext
    from domoai.optimizer.scenario import OptimizationScenario, scenario_definition_digest

    schema_path = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "contracts"
        / ("energy-context-before-base-source.schema.json")
    )
    legacy = Draft202012Validator(json.loads(schema_path.read_text()))
    context = composer(None).get_context(horizon())
    legacy.validate(context.model_dump(mode="json"))
    scenario = OptimizationScenario(
        id="legacy-compatible", horizon=horizon(), energy_context=context
    )
    nested = scenario.model_dump(mode="json")["energy_context"]
    legacy.validate(nested)
    assert "base_load_source" not in nested
    assert EnergyContext.model_validate(nested) == context
    # Captured from the pre-A09 shape after exact old-reader validation.
    assert scenario_definition_digest(scenario) == (
        "sha256:41505f310b443f4fa97ed926f4ed7c578cfba44d709fb07f95d16af7d18aafa8"
    )


def test_provided_source_requires_updated_reader_and_survives_roundtrip(tmp_path: Path) -> None:
    from jsonschema import Draft202012Validator

    from domoai.optimizer.energy import EnergyContext

    provider = DeclaredBaseLoadProvider.from_file(
        write_profile(tmp_path / "source.json"), tenant_id="tenant-a", household_id="home-a"
    )
    context = composer(provider).get_context(horizon())
    payload = context.model_dump(mode="json")
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "fixtures"
            / "contracts"
            / "energy-context-before-base-source.schema.json"
        ).read_text()
    )
    assert list(Draft202012Validator(schema).iter_errors(payload))
    assert payload["base_load_source"]["kind"] == "declared_reserve"
    assert EnergyContext.model_validate_json(context.model_dump_json()) == context


def test_provenance_selects_v2_context_and_scenario_without_explicit_version(
    tmp_path: Path,
) -> None:
    from domoai.optimizer.energy import EnergyContext
    from domoai.optimizer.scenario import OptimizationScenario, scenario_definition_digest

    provider = DeclaredBaseLoadProvider.from_file(
        write_profile(tmp_path / "v2.json"), tenant_id="tenant-a", household_id="home-a"
    )
    context = composer(provider).get_context(horizon())
    assert context.schema_version == "v2"
    scenario = OptimizationScenario(id="versioned", horizon=horizon(), energy_context=context)
    assert scenario.schema_version == "v2"
    assert scenario.model_dump(mode="json")["energy_context"]["base_load_source"]
    assert OptimizationScenario.model_validate_json(scenario.model_dump_json()) == scenario
    assert EnergyContext.model_validate_json(context.model_dump_json()) == context
    implicit = context.model_dump(mode="json")
    implicit.pop("schema_version")
    assert EnergyContext.model_validate(implicit).schema_version == "v2"
    legacy = composer(None).get_context(horizon())
    old_scenario = OptimizationScenario(id="versioned", horizon=horizon(), energy_context=legacy)
    assert old_scenario.schema_version == "v1"
    assert scenario_definition_digest(scenario) != scenario_definition_digest(old_scenario)


@pytest.mark.parametrize(
    "version,provided,accepted",
    [
        ("v1", False, True),
        ("v1", True, False),
        ("v2", False, False),
        ("v2", True, True),
    ],
)
def test_context_versions_reject_explicit_downgrade_and_missing_provenance(
    version,
    provided,
    accepted,
) -> None:
    from domoai.optimizer.energy import EnergyContext
    from tests.unit.optimizer.test_base_load import BaseProvider

    context = composer(BaseProvider(base_series())).get_context(horizon())
    payload = context.model_dump(mode="json")
    payload["schema_version"] = version
    if not provided:
        payload["base_load_source"] = None
    if accepted:
        parsed = EnergyContext.model_validate(payload)
        assert parsed.schema_version == version
        assert ("base_load_source" in parsed.model_dump(mode="json")) is provided
    else:
        with pytest.raises(ValidationError):
            EnergyContext.model_validate(payload)


@pytest.mark.parametrize("outer,inner", [("v1", "v2"), ("v2", "v1")])
def test_scenario_rejects_explicit_nested_version_mismatch(outer, inner) -> None:
    from domoai.optimizer.scenario import OptimizationScenario
    from tests.unit.optimizer.test_base_load import BaseProvider

    context = composer(BaseProvider(base_series()) if inner == "v2" else None).get_context(
        horizon()
    )
    with pytest.raises(ValidationError):
        OptimizationScenario(
            schema_version=outer, id="mixed", horizon=horizon(), energy_context=context
        )


def test_published_v1_artifacts_remain_exact_and_v2_payloads_validate(tmp_path: Path) -> None:
    from jsonschema import Draft202012Validator

    from domoai.optimizer.scenario import OptimizationScenario

    repository = Path(__file__).resolve().parents[2]
    frozen = repository / "tests" / "fixtures" / "contracts"
    for name in ["energy-context", "optimization-scenario"]:
        current = repository / "schemas" / "v1" / f"{name}.schema.json"
        assert (
            current.read_bytes() == (frozen / f"{name}-before-base-source.schema.json").read_bytes()
        )
    provider = DeclaredBaseLoadProvider.from_file(
        write_profile(tmp_path / "versioned.json"), tenant_id="tenant-a", household_id="home-a"
    )
    context = composer(provider).get_context(horizon())
    scenario = OptimizationScenario(
        id="versioned-schema", horizon=horizon(), energy_context=context
    )
    for name, model in [("energy-context", context), ("optimization-scenario", scenario)]:
        schema_file = repository / "schemas" / "v2" / f"{name}.schema.json"
        assert schema_file.exists(), f"missing v2 {name} schema"
        validator = Draft202012Validator(json.loads(schema_file.read_text()))
        payload = model.model_dump(mode="json")
        validator.validate(payload)
        payload["schema_version"] = "v1"
        assert list(validator.iter_errors(payload))


def test_legacy_scenario_unknown_outer_label_remains_permissive() -> None:
    from domoai.optimizer.scenario import OptimizationScenario

    # The old outer schema was str, unlike the literal version in its context.
    context = composer(None).get_context(horizon())
    scenario = OptimizationScenario(
        schema_version="legacy-custom", id="legacy-label", horizon=horizon(), energy_context=context
    )
    assert scenario.schema_version == "legacy-custom"


def test_versioned_schema_views_match_canonical_runtime_payloads() -> None:
    from jsonschema import Draft202012Validator
    from scripts.export_schemas import MODELS, V2_MODELS

    from domoai.optimizer.energy import EnergyContext
    from domoai.optimizer.scenario import OptimizationScenario
    from tests.unit.optimizer.test_base_load import BaseProvider

    for version, provider in [("v1", None), ("v2", BaseProvider(base_series()))]:
        context = composer(provider).get_context(horizon())
        scenario = OptimizationScenario(
            id="version-parity", horizon=horizon(), energy_context=context
        )
        published = MODELS if version == "v1" else V2_MODELS
        for name, runtime, instance in [
            ("energy-context", EnergyContext, context),
            ("optimization-scenario", OptimizationScenario, scenario),
        ]:
            payload = instance.model_dump(mode="json")
            view = published[name]
            schema = view.model_json_schema()
            Draft202012Validator(schema).validate(payload)
            assert view.model_validate(payload).model_dump(mode="json") == payload
            assert runtime.model_validate(payload).model_dump(mode="json") == payload
            if version == "v2":
                if name == "energy-context":
                    malformed = {**payload, "base_load_source": None}
                else:
                    malformed = {
                        **payload,
                        "energy_context": composer(None)
                        .get_context(horizon())
                        .model_dump(mode="json"),
                    }
                assert list(Draft202012Validator(schema).iter_errors(malformed))
                with pytest.raises(ValidationError):
                    runtime.model_validate(malformed)
                with pytest.raises(ValidationError):
                    view.model_validate(malformed)
    # Explicit None is a reader convenience. Canonical v1 always omits it.
    legacy = composer(None).get_context(horizon()).model_dump(mode="json")
    assert (
        EnergyContext.model_validate({**legacy, "base_load_source": None}).model_dump(mode="json")
        == legacy
    )
