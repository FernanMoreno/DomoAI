from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.coordination import DeterministicLeaseCoordinator
from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.domain.automation import (
    AutomationConsent,
    AutomationEvent,
    AutomationRule,
    AutomationTrigger,
    automation_rule_digest,
)
from domoai.domain.models import Command


@pytest.mark.composition
@pytest.mark.asyncio
async def test_scheduler_rule_replay_after_restart_uses_one_physical_ledger_intent(
    tmp_path: Path,
) -> None:
    class FencedSimulatedHomeAdapter(SimulatedHomeAdapter):
        supports_fencing = True

    database_path = tmp_path / "time-rule.sqlite3"
    first_adapter = FencedSimulatedHomeAdapter()
    coordinator = DeterministicLeaseCoordinator()
    first_runtime = await build_runtime(
        Settings(database_path=database_path, multi_host_enabled=True),
        adapter=first_adapter,
        lease_coordinator=coordinator,
    )
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    try:
        light_id = next(
            device.id
            for device in first_runtime.registry.devices
            if device.type.value == "light"
        )
        rule = AutomationRule(
            id="restart-time-rule",
            name="Restart-safe time rule",
            trigger=AutomationTrigger(type="time", time_of_day=now.time(), timezone="UTC"),
            plan_template=first_runtime.plan_service.create_plan(
                "restart-time-template",
                [
                    Command(
                        id="restart-time-command",
                        device_id=light_id,
                        command="turn_on",
                        idempotency_key="restart-time-template-key",
                    )
                ],
            ),
            scope="home",
            expires_at=now + timedelta(hours=1),
            status="enabled",
        )
        await first_runtime.local_automation.register(
            rule,
            AutomationConsent(
                approval_id="restart-time-approval",
                principal_id="operator",
                scope=rule.scope,
                rule_digest=automation_rule_digest(rule),
                approved_at=now - timedelta(minutes=1),
                expires_at=now + timedelta(hours=1),
            ),
        )

        result = await first_runtime.local_automation.evaluate_time(now)
        plan = await first_runtime.plan_repository.get(result[0].plan_id)
        intent_key = plan.commands[0].idempotency_key
        assert len(first_adapter.calls) == 1
        assert await first_runtime.physical_intent_repository.get(
            household_id=rule.authority.household_id, idempotency_key=intent_key
        ) is not None
    finally:
        await first_runtime.close()

    second_adapter = FencedSimulatedHomeAdapter()
    second_runtime = await build_runtime(
        Settings(database_path=database_path, multi_host_enabled=True),
        adapter=second_adapter,
        lease_coordinator=coordinator,
    )
    try:
        replay = await second_runtime.local_automation.handle_event(
            AutomationEvent(
                event_id="caller-changed-after-restart",
                event_type="time",
                target_rule_id="restart-time-rule",
                occurred_at=now,
            )
        )

        assert replay[0].status == "duplicate"
        assert len(second_adapter.calls) == 0
        assert await second_runtime.physical_intent_repository.get(
            household_id="default", idempotency_key=intent_key
        ) is not None
    finally:
        await second_runtime.close()
