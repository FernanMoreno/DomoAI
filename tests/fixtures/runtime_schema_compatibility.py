"""Exact historical readers and deterministic compatibility payloads."""

import hashlib
import json
import sys
import types
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from domoai.domain.models import Command, Plan

FIXTURES = Path(__file__).parent / "contracts" / "runtime-schema-compatibility"
NOW = datetime(2026, 9, 11, tzinfo=UTC)


def plan(**changes: object) -> Plan:
    values: dict[str, object] = {
        "id": "compat-plan",
        "created_at": NOW,
        "commands": [
            Command(id="cmd", device_id="device", command="turn_on", idempotency_key="intent")
        ],
    }
    return Plan.model_validate(values | changes)


@lru_cache
def historical_readers() -> dict[str, type]:
    provenance = json.loads((FIXTURES / "provenance.json").read_text())
    modules = {}
    for short in ("models", "automation", "coordination", "optimizer_ports"):
        path = FIXTURES / f"{short}.py.txt"
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == provenance["files"][path.name]["sha256"]
        source = raw.decode().replace(
            "from domoai.domain.models import", "from schema253_legacy_models import"
        )
        name = f"schema253_legacy_{short}"
        module = types.ModuleType(name)
        sys.modules[name] = module
        exec(compile(source, str(path), "exec"), module.__dict__)
        modules[short] = module
    return {
        "plan": modules["models"].Plan,
        "automation-rule": modules["automation"].AutomationRule,
        "automation-event": modules["automation"].AutomationEvent,
        "physical-intent": modules["coordination"].PhysicalIntent,
        "optimization-result": modules["optimizer_ports"].OptimizationResult,
    }


async def seed_related_history(database) -> dict[str, list[tuple]]:
    """Seed authentic typed rule/consent/grant/ledger and retained schedule evidence."""
    from datetime import timedelta

    from domoai.domain.coordination import PhysicalIntent, PhysicalIntentStatus
    from domoai.domain.models import BundleCommit, BundleMemberCommit
    from domoai.persistence.coordination import PhysicalIntentRepository
    from domoai.persistence.repositories import (
        ApprovalGrantRepository,
        AutomationRuleRepository,
        BundleCommitRepository,
    )
    from domoai.runtime.approval_store import ApprovalGrant
    from tests.integration.test_local_automation_persistence import _record
    from tests.integration.test_runtime_schema_compatibility import historical_payload

    raw = historical_payload(record_id="scheduled-history")
    database.connection.execute(
        "INSERT INTO scheduled_plans VALUES(?,?,?,?,?)",
        ("scheduled-history", NOW.isoformat(), "pending", raw, NOW.isoformat()),
    )
    database.connection.commit()
    rule, consent = _record()
    await AutomationRuleRepository(database).save(rule, consent)
    await BundleCommitRepository(database).save(
        BundleCommit(
            id="history-bundle",
            scenario_id="history",
            bundle_digest="original-bundle",
            approval_reservation_id="reservation",
            members=[
                BundleMemberCommit(plan_id="approved", validation_digest="original-validation")
            ],
        )
    )
    grants = ApprovalGrantRepository(database)
    grant = ApprovalGrant(
        approval_id="historical-grant",
        plan_id="approved",
        validation_digest="original-validation",
        approved_by="operator",
        issued_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        assertion_nonce="original-nonce",
    )
    grants.save_sync(grant)
    assert grants.consume_if_pending_sync(grant.approval_id, now=NOW)
    ledger = PhysicalIntentRepository(database)
    for status in (
        PhysicalIntentStatus.DISPATCHING,
        PhysicalIntentStatus.ACKNOWLEDGED,
        PhysicalIntentStatus.CONFIRMED,
        PhysicalIntentStatus.UNKNOWN,
    ):
        intent = PhysicalIntent(
            tenant_id="default",
            household_id="default",
            deployment_id="deployment",
            idempotency_key="history-" + status.value,
            plan_id="executing",
            command_id="command",
            fencing_epoch=7,
            owner_id="original-owner",
            execution_attempt_id="original-attempt",
            lease_id="original-lease",
            created_at=NOW,
            updated_at=NOW,
        )
        claimed = await ledger.claim_for_dispatch(intent)
        if status != PhysicalIntentStatus.DISPATCHING:
            await ledger.settle(intent=claimed.intent, status=status)
    return {
        table: database.connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        for table in (
            "scheduled_plans",
            "automation_rules",
            "approval_grants",
            "bundle_commits",
            "physical_intents",
        )
    }
