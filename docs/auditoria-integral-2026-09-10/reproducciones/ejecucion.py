import asyncio
import tempfile
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from domoai.application.household_queue import HouseholdWorkQueues
from domoai.application.local_automation import LocalAutomationEngine
from domoai.domain.automation import AutomationConsent, AutomationRule, AutomationTrigger
from domoai.domain.coordination import PhysicalIntent, PhysicalIntentStatus
from domoai.domain.models import Command, ExecutionSummary, Plan, PlanStatus
from domoai.persistence.coordination import PhysicalIntentRepository
from domoai.persistence.repositories import AutomationRuleRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.events import AuditLog


class Plans:
    def create_plan(self, plan_id, commands, *, expires_at=None):
        return Plan(id=plan_id, commands=commands, expires_at=expires_at)

    def validate(self, plan):
        return plan.model_copy(update={"status": PlanStatus.READY})


class Executor:
    def __init__(self):
        self.plans = []

    async def execute(self, plan):
        self.plans.append(plan.id)
        return ExecutionSummary(outcomes=[])


async def main():
    with tempfile.TemporaryDirectory(prefix="domoai-audit-") as d:
        db = SQLiteDatabase(Path(d) / "proof.sqlite")
        await db.initialize()
        repo = PhysicalIntentRepository(db)
        now = datetime.now(UTC)
        intent = PhysicalIntent(
            tenant_id="default",
            household_id="default",
            deployment_id="edge",
            idempotency_key="key",
            plan_id="p",
            command_id="c",
            fencing_epoch=1,
            owner_id="a",
            execution_attempt_id="attempt",
            created_at=now,
            updated_at=now,
        )
        claim = await repo.claim_for_dispatch(intent)
        await repo.settle(intent=claim.intent, status=PhysicalIntentStatus.ACKNOWLEDGED)
        await repo.settle(intent=claim.intent, status=PhysicalIntentStatus.CONFIRMED)
        print(
            "ledger_after_confirmed=",
            (await repo.get(household_id="default", idempotency_key="key")).status.value,
        )
        print(
            "recovery_reclassified_successes=",
            await repo.recover_inflight(deployment_id="edge", fencing_epoch=2),
        )
        print(
            "ledger_after_recovery=",
            (await repo.get(household_id="default", idempotency_key="key")).status.value,
        )
        now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
        rules = AutomationRuleRepository(db)
        executor = Executor()
        engine = LocalAutomationEngine(rules, Plans(), executor, AuditLog(), clock=lambda: now)
        for hour in [12, 18]:
            rule = AutomationRule(
                id=f"rule{hour}",
                name=f"At {hour}",
                trigger=AutomationTrigger(type="time", time_of_day=time(hour), timezone="UTC"),
                plan_template=Plan(
                    id=f"template{hour}",
                    commands=[
                        Command(
                            id=f"c{hour}",
                            device_id="light.one",
                            command="turn_on",
                            idempotency_key=f"k{hour}",
                        )
                    ],
                ),
                scope="home",
                status="enabled",
            )
            consent = AutomationConsent(
                approval_id=f"approval{hour}",
                principal_id="operator",
                scope="home",
                rule_digest=rule.definition_digest,
                approved_at=now - timedelta(hours=1),
                expires_at=now + timedelta(days=1),
            )
            await engine.register(rule, consent)
        await engine.evaluate_time(now)
        print("time_12_plans=", executor.plans)
        await db.close()
    queues = HouseholdWorkQueues()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def block():
        entered.set()
        await release.wait()

    first = asyncio.create_task(queues.submit("home", block))
    await entered.wait()
    second = asyncio.create_task(queues.submit("home", lambda: calls.append("PHYSICAL_WRITE")))
    await asyncio.sleep(0)
    second.cancel()
    try:
        await second
    except asyncio.CancelledError:
        pass
    release.set()
    await first
    await queues.close()
    print("cancelled_queue_calls=", calls)


asyncio.run(main())
