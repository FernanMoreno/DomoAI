"""Opt-in end-to-end EV composition against the disposable lab stack."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.domain.models import (
    Command,
    CommandPostcondition,
    ExecutionStatus,
    Plan,
    RiskClass,
)

if TYPE_CHECKING:
    from domoai.application.runtime_factory import RuntimeComposition


def _attended_approved(
    runtime: RuntimeComposition, plan: Plan, *, session_id: str
) -> Plan:
    validated = runtime.facade.validate_plan(plan)
    assert validated.validation is not None
    grant = runtime.approval_store.issue_attended_local(
        validated,
        operator_id="live-ev-composition-operator",
        session_id=session_id,
    )
    consumed = runtime.approval_store.consume(grant.approval_id, validated)
    return runtime.facade.approve_plan(validated, grant=consumed)


def _ev_plan(
    *,
    plan_id: str,
    device_id: str,
    command: str,
    value: float | None,
    expected: float,
    execute_at: datetime | None = None,
) -> Plan:
    return Plan(
        id=plan_id,
        execute_at=execute_at,
        commands=[
            Command(
                id=f"{plan_id}:command",
                device_id=device_id,
                command=command,
                value=value,
                unit="kW" if value is not None else None,
                risk_class=RiskClass.CONFIRM,
                idempotency_key=f"{plan_id}:intent",
                postconditions=[
                    CommandPostcondition(
                        capability="ev_charging",
                        expected=expected,
                        tolerance=0.1,
                        settle_timeout_seconds=10.0,
                        poll_interval_seconds=0.25,
                    )
                ],
            )
        ],
    )


@pytest.mark.composition
@pytest.mark.asyncio
async def test_live_ev_event_scheduler_actuator_composition(tmp_path: Path) -> None:
    if os.getenv("DOMOAI_LIVE_EV_COMPOSITION_ENABLE") != "1":
        pytest.skip("set DOMOAI_LIVE_EV_COMPOSITION_ENABLE=1 for the real EV composition")
    required = (
        "DOMOAI_HOME_ASSISTANT_URL",
        "DOMOAI_HOME_ASSISTANT_TOKEN",
        "DOMOAI_HOME_ASSISTANT_MAPPING_PATH",
        "DOMOAI_EV_CHARGING_BINDING_PATHS",
    )
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        pytest.skip("live EV configuration is incomplete: " + ", ".join(missing))

    settings = Settings.from_environment().model_copy(
        update={
            "database_path": tmp_path / "live-ev.sqlite3",
            "audit_database_path": tmp_path / "live-ev-audit.sqlite3",
            "bootstrap_manifest_path": None,
        }
    )
    runtime = await build_runtime(settings)
    stop_needed = False
    try:
        await runtime.start()
        health = await runtime.adapter.health()
        assert health.connected
        assert runtime.ev_actuators
        actuator = runtime.ev_actuators[0]
        connected = await runtime.state_store.get(
            actuator.device_id, actuator.connected_capability
        )
        assert connected is not None and connected.value is True

        charge_id = "live-ev-scheduled-charge"
        scheduled = _attended_approved(
            runtime,
            _ev_plan(
                plan_id=charge_id,
                device_id=actuator.device_id,
                command=actuator.charge_command,
                value=1.0,
                expected=1.0,
                execute_at=datetime.now(UTC) - timedelta(seconds=1),
            ),
            session_id="live-ev-scheduled-charge-session",
        )
        # From this point onward the scheduler may cross the physical write
        # boundary; cleanup must therefore always attempt the fail-safe stop.
        stop_needed = True
        await runtime.plan_repository.save(scheduled)
        await runtime.scheduler.schedule(scheduled)
        results = await runtime.scheduler.run_due()
        assert results == [{"plan_id": charge_id, "outcome": "executed"}]
        persisted = await runtime.plan_repository.get(charge_id)
        assert persisted is not None and persisted.execution is not None
        assert persisted.execution.outcomes[0].status is ExecutionStatus.CONFIRMED_SUCCESS
        scheduled_row = await runtime.scheduled_plan_repository.get(charge_id)
        assert scheduled_row is not None and scheduled_row[1] == "executed"
        assert runtime.event_consumer.events_applied > 0

    finally:
        try:
            if stop_needed and runtime.ev_actuators:
                actuator = runtime.ev_actuators[0]
                stop_id = "live-ev-composition-stop"
                stop = _attended_approved(
                    runtime,
                    _ev_plan(
                        plan_id=stop_id,
                        device_id=actuator.device_id,
                        command=actuator.stop_command,
                        value=None,
                        expected=0.0,
                    ),
                    session_id="live-ev-composition-stop-session",
                )
                summary = await runtime.facade.execute_plan(stop)
                assert summary.outcomes[0].status is ExecutionStatus.CONFIRMED_SUCCESS
        finally:
            await runtime.close()
