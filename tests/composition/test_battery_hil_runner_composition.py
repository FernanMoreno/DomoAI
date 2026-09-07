"""domoai.hil.runner drives a real check sequence, not a hand-typed evidence
file (closes P1.7 from the 2026-08-24 re-audit of commit 61439f3).

`BatteryFixtureAdapter` below is deliberately a *dynamic* fixture: unlike
`RecordingAdapter`, it actually updates its backing state on each
charge/discharge/stop write, so the runner's postcondition polling observes
a real (simulated) physical transition -- proving the evidence this test
produces reflects executor outcomes, not assertions the test wrote by hand.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from domoai.application.runtime_factory import build_runtime
from domoai.config.battery_qualification import BatteryHILEvidence
from domoai.config.settings import Settings
from domoai.domain.models import (
    AdapterExecutionAck,
    Command,
    ControlLeaseStatus,
    PhysicalBaseline,
    Plan,
    SourceRef,
    StateStatus,
    TakeoverResult,
)
from domoai.domain.provider import MeasurementQuality
from domoai.hil.runner import BatteryHILRunError, run_battery_hil
from domoai.optimizer.energy import (
    BatteryActuator,
    BatteryCapacityEvidence,
    BatteryControlPolicy,
    BatteryProfile,
    BatterySocObservation,
    DispatchableBatteryBinding,
)
from domoai.runtime.approval_store import ApprovalGrant
from domoai.runtime.control_takeover import ControlTakeoverRequest
from domoai.runtime.execution_context import ExecutionContext
from tests.fixtures.multi_adapter import RecordingAdapter, entity, source_snapshot


class BatteryFixtureAdapter(RecordingAdapter):
    """Actually mutates backing state on write, like a real inverter would."""

    async def execute_source(
        self,
        command: Command,
        source_entity_id: str,
        execution_context: ExecutionContext | None = None,
    ) -> AdapterExecutionAck:
        if not self.connected or not self.available:
            return AdapterExecutionAck(accepted=False, message="Fixture source unavailable")
        self.writes.append((source_entity_id, command))
        for state in self.snapshot.source_states:
            # PlanExecutor dispatches through the canonical command shape;
            # match purely on capability (there is only one battery fixture
            # device in this snapshot) rather than re-deriving the source
            # entity id translation PlanExecutor already resolved.
            if state["capability"] != "battery.power":
                continue
            if command.command == "charge_battery":
                state["value"] = float(command.value or 0.0)
            elif command.command == "discharge_battery":
                state["value"] = -float(command.value or 0.0)
            else:  # stop_battery
                state["value"] = 0.0
        return AdapterExecutionAck(
            accepted=True,
            source_ref=SourceRef(adapter_id=self.adapter_id, external_id=source_entity_id),
            message="Fixture source accepted",
        )

    async def acquire_control(self, request: ControlTakeoverRequest) -> TakeoverResult:
        now = datetime.now(UTC)
        return TakeoverResult(
            lease_id=f"fixture-{request.plan_id}",
            status=ControlLeaseStatus.ACQUIRED,
            owner=request.owner,
            device_id=request.device_id,
            plan_id=request.plan_id,
            acquired_at=now,
            expires_at=now + timedelta(seconds=request.lease_seconds),
            baseline=PhysicalBaseline(
                device_id=request.device_id,
                capability="battery.power",
                power_kw=0.0,
                observed_at=now,
                received_at=now,
                source_ref=SourceRef(adapter_id=self.adapter_id, external_id="battery.fixture"),
                state_revision="fixture:baseline",
                native_scheduler_status="disabled",
            ),
            first_command_id=request.first_command_id,
            evidence_digest="sha256:fixture-takeover",
        )


def _battery_snapshot():
    battery_entity = entity(
        entity_id="battery.fixture",
        source_device_id="battery-fixture-1",
        canonical_id="energy.battery-fixture",
        name="Fixture Battery",
        area_id="energy",
        capabilities=[
            {
                "name": "battery.power",
                "kind": "number",
                "unit": "kW",
                "readable": True,
                "writable": True,
                "commands": ["charge_battery", "discharge_battery", "stop_battery"],
            },
            {
                "name": "battery.soc",
                "kind": "number",
                "unit": "kWh",
                "readable": True,
                "writable": False,
                "commands": [],
            },
        ],
    )
    snapshot = source_snapshot(adapter_id="fixture", include_shared_device=False)
    snapshot = snapshot.model_copy(
        update={
            "source_entities": [*snapshot.source_entities, battery_entity],
            "source_states": [
                *snapshot.source_states,
                {
                    "entity_id": "battery.fixture",
                    "capability": "battery.power",
                    "value": 0.0,
                    "unit": "kW",
                },
                {
                    "entity_id": "battery.fixture",
                    "capability": "battery.soc",
                    "value": 4.0,
                    "unit": "kWh",
                },
            ],
        }
    )
    return snapshot


def _battery_device_id(runtime) -> str:
    # The registry computes its own canonical device id from source identity
    # (see DeviceRegistry._canonical_id_for) -- the raw entity()'s
    # "canonical_id" field is descriptive only and not consumed by it, so
    # the real id must be looked up post-discovery, same as every other
    # composition test in this suite does.
    return next(
        device.id for device in runtime.registry.devices if device.name == "Fixture Battery"
    )


def _binding(device_id: str) -> DispatchableBatteryBinding:
    observed_at = datetime(2026, 8, 24, 12, tzinfo=UTC)
    return DispatchableBatteryBinding(
        provider_id="fixture",
        device_id=device_id,
        profile=BatteryProfile(
            capacity_kwh=8.0,
            initial_soc_kwh=4.0,
            min_soc_kwh=0.0,
            max_soc_kwh=8.0,
            max_charge_kw=2.0,
            max_discharge_kw=2.0,
            charge_efficiency=1.0,
            discharge_efficiency=1.0,
            actuator=BatteryActuator(
                device_id=device_id,
                capability="battery.power",
                charge_command="charge_battery",
                discharge_command="discharge_battery",
                stop_command="stop_battery",
                power_feedback_capability="battery.power",
                power_feedback_tolerance_kw=0.05,
                soc_reconciliation_capability="battery.soc",
            ),
            initial_soc_observation=BatterySocObservation(
                provider_id="fixture",
                device_id=device_id,
                metric="battery.soc",
                value_kwh=4.0,
                observed_at=observed_at,
                received_at=observed_at,
                quality=MeasurementQuality.GOOD,
                source_ref=SourceRef(adapter_id="fixture", external_id="battery.fixture"),
            ),
        ),
        capacity_evidence=BatteryCapacityEvidence(
            provider_id="fixture",
            device_id=device_id,
            capacity_kwh=8.0,
            capacity_source="provider_config",
            quality=MeasurementQuality.GOOD,
            observed_at=observed_at,
            received_at=observed_at,
            source_ref=SourceRef(adapter_id="fixture", external_id="battery.fixture"),
        ),
        control_policy=BatteryControlPolicy(native_scheduler_status="disabled"),
    )


def _settings(path) -> Settings:
    return Settings(
        database_path=path,
        energy_live=True,
        tariff_provider="omie",
        solar_provider="open_meteo",
        solar_latitude=40.4168,
        solar_longitude=-3.7038,
        solar_installed_kwp=6.0,
        solar_tilt=30.0,
        solar_azimuth=0.0,
        solar_performance_ratio=0.82,
        battery_hil_power_ceiling_kw=1.0,
    )


_ATTESTATIONS = {
    "native_scheduler_conflict": "native scheduler disabled on fixture bench",
    "restart_no_replay": "not exercised by this in-process fixture run",
}


@pytest.mark.composition
@pytest.mark.asyncio
async def test_hil_runner_marks_fixture_evidence_non_qualifying_without_manual_proof(
    tmp_path,
) -> None:
    adapter = BatteryFixtureAdapter("fixture", _battery_snapshot())
    probe = await build_runtime(
        _settings(tmp_path / "hil-probe.sqlite3"), adapter=adapter
    )
    device_id = _battery_device_id(probe)
    await probe.close()
    binding = _binding(device_id)
    runtime = await build_runtime(
        _settings(tmp_path / "hil-runtime.sqlite3"),
        adapter=adapter,
        dispatchable_battery_binding=binding,
    )
    try:
        evidence = await run_battery_hil(
            runtime,
            binding=binding,
            test_charge_kw=0.5,
            test_discharge_kw=0.5,
            hardware_id="fixture-serial-1",
            firmware_version="0.0.1-fixture",
            test_software_version="test-sha",
            manual_attestations=_ATTESTATIONS,
        )
    finally:
        await runtime.close()

    assert evidence.status == "failed"
    assert evidence.checks["takeover_baseline"] is True
    assert evidence.checks["restart_no_replay"] is False
    assert evidence.manual_check_status["restart_no_replay"] == "not_exercised"
    assert evidence.checks.keys() == {
        "identity",
        "writable_routes",
        "takeover_baseline",
        "charge_feedback",
        "discharge_feedback",
        "stop_feedback",
        "polarity",
        "soc_reconciliation",
        "native_scheduler_conflict",
        "restart_no_replay",
    }
    # The evidence is derived from real dispatch, not an assertion this test
    # wrote directly: the adapter actually saw the writes.
    commands = [command.command for _entity_id, command in adapter.writes]
    # Every standalone HIL step owns and then releases its lease.  Release is
    # a provider-side stop with readback, so the baseline stop is followed by
    # a second idempotent stop before the charge step begins.
    assert commands[:10] == [
        "stop_battery",
        "stop_battery",
        "stop_battery",
        "charge_battery",
        "stop_battery",
        "stop_battery",
        "stop_battery",
        "discharge_battery",
        "stop_battery",
        "stop_battery",
        "stop_battery",
    ]
    # Runtime shutdown also releases every still-recorded lease defensively;
    # those additional writes are all idempotent stop commands.
    assert all(command == "stop_battery" for command in commands[10:])
    assert evidence.observations["charge_feedback"]["status"] == "confirmed_success"
    assert evidence.manual_attestations == _ATTESTATIONS
    assert evidence.manual_check_status["restart_no_replay"] == "not_exercised"
    assert evidence.test_software_version == "test-sha"
    # Re-validating the serialized form proves this is a real BatteryHILEvidence
    # artifact, not just a Python object shaped like one.
    reloaded = BatteryHILEvidence.model_validate_json(evidence.model_dump_json())
    assert not reloaded.qualifies(binding)


@pytest.mark.composition
@pytest.mark.asyncio
async def test_runtime_dynamic_guard_blocks_stale_battery_before_adapter_write(tmp_path) -> None:
    adapter = BatteryFixtureAdapter("fixture", _battery_snapshot())
    probe = await build_runtime(
        _settings(tmp_path / "guard-probe.sqlite3"), adapter=adapter
    )
    device_id = _battery_device_id(probe)
    await probe.close()
    binding = _binding(device_id)
    runtime = await build_runtime(
        _settings(tmp_path / "guard-runtime.sqlite3"),
        adapter=adapter,
        dispatchable_battery_binding=binding,
    )
    try:
        power = await runtime.state_store.get(device_id, "battery.power")
        assert power is not None
        stale_at = runtime.clock.now() - runtime.state_store.stale_after - timedelta(seconds=1)
        await runtime.state_store.save(
            power.model_copy(update={"observed_at": stale_at, "status": StateStatus.CURRENT})
        )
        plan = runtime.plan_service.validate(
            Plan(
                id="stale-battery-write",
                commands=[
                    Command(
                        id="stale-battery-charge",
                        device_id=device_id,
                        command="charge_battery",
                        value=0.5,
                        unit="kW",
                        idempotency_key="stale-battery-charge-key",
                    )
                ],
            )
        )
        assert plan.validation is not None
        plan = runtime.facade.approve_plan(
            plan,
            grant=ApprovalGrant(
                approval_id="stale-battery-approval",
                plan_id=plan.id,
                validation_digest=plan.validation.digest,
                approved_by="composition-test-operator",
                issued_at=runtime.clock.now(),
                validation_valid_until=plan.validation.valid_until,
                expires_at=plan.validation.valid_until,
            ),
        )
        await runtime.plan_repository.save_validation(plan)
        writes_before = list(adapter.writes)

        summary = await runtime.facade.execute_plan(plan)

        assert summary.outcomes[0].status.value == "rejected"
        assert summary.outcomes[0].error is not None
        assert summary.outcomes[0].error.details["authority"] == "dynamic_safety_guard"
        assert adapter.writes == writes_before
    finally:
        await runtime.close()


@pytest.mark.composition
@pytest.mark.asyncio
async def test_hil_runner_refuses_to_run_without_required_manual_attestations(
    tmp_path,
) -> None:
    adapter = BatteryFixtureAdapter("fixture", _battery_snapshot())
    runtime = await build_runtime(
        Settings(database_path=tmp_path / "hil-runtime-missing.sqlite3"), adapter=adapter
    )
    try:
        with pytest.raises(BatteryHILRunError, match="manual attestation"):
            await run_battery_hil(
                runtime,
                binding=_binding(_battery_device_id(runtime)),
                test_charge_kw=0.5,
                test_discharge_kw=0.5,
                hardware_id="fixture-serial-1",
                firmware_version="0.0.1-fixture",
                test_software_version=None,
                manual_attestations={},
            )
        # Refusing to run means zero dispatch attempts too.
        assert adapter.writes == []
    finally:
        await runtime.close()


@pytest.mark.composition
@pytest.mark.asyncio
async def test_hil_runner_requires_server_owned_power_ceiling_before_commands(tmp_path) -> None:
    adapter = BatteryFixtureAdapter("fixture", _battery_snapshot())
    probe = await build_runtime(_settings(tmp_path / "ceiling-probe.sqlite3"), adapter=adapter)
    device_id = _battery_device_id(probe)
    await probe.close()
    binding = _binding(device_id)
    runtime = await build_runtime(
        _settings(tmp_path / "ceiling-runtime.sqlite3").model_copy(
            update={"battery_hil_power_ceiling_kw": None}
        ),
        adapter=adapter,
        dispatchable_battery_binding=binding,
    )
    try:
        writes_before = list(adapter.writes)
        with pytest.raises(BatteryHILRunError, match="safety ceiling"):
            await run_battery_hil(
                runtime,
                binding=binding,
                test_charge_kw=0.5,
                test_discharge_kw=0.5,
                hardware_id="fixture-serial-1",
                firmware_version="0.0.1-fixture",
                test_software_version="test-sha",
                manual_attestations=_ATTESTATIONS,
            )
        assert adapter.writes == writes_before
    finally:
        await runtime.close()


@pytest.mark.composition
@pytest.mark.asyncio
async def test_hil_runner_reports_failed_status_when_a_check_fails(tmp_path) -> None:
    adapter = BatteryFixtureAdapter("fixture", _battery_snapshot())
    # Break the adapter for the discharge command specifically: it will
    # accept the write but never actually move the feedback capability, so
    # the postcondition cannot confirm.
    original_execute_source = adapter.execute_source

    async def _flaky_execute_source(command, source_entity_id, execution_context=None):
        if command.command == "discharge_battery":
            return AdapterExecutionAck(accepted=False, message="simulated discharge failure")
        return await original_execute_source(command, source_entity_id, execution_context)

    adapter.execute_source = _flaky_execute_source  # type: ignore[method-assign]

    probe = await build_runtime(
        _settings(tmp_path / "hil-probe-failed.sqlite3"), adapter=adapter
    )
    device_id = _battery_device_id(probe)
    await probe.close()
    binding = _binding(device_id)
    runtime = await build_runtime(
        _settings(tmp_path / "hil-runtime-failed.sqlite3"),
        adapter=adapter,
        dispatchable_battery_binding=binding,
    )
    try:
        evidence = await run_battery_hil(
            runtime,
                binding=binding,
            test_charge_kw=0.5,
            test_discharge_kw=0.5,
            hardware_id="fixture-serial-1",
            firmware_version="0.0.1-fixture",
            test_software_version=None,
            manual_attestations=_ATTESTATIONS,
        )
    finally:
        await runtime.close()

    assert evidence.status == "failed"
    assert evidence.checks["discharge_feedback"] is False
    assert evidence.checks["charge_feedback"] is True
    # A "failed" evidence file must not be able to qualify a real binding.
    assert evidence.qualifies(_binding(_battery_device_id(runtime))) is False
