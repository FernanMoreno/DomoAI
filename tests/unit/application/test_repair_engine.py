from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from domoai.application.repair_engine import RepairEngine, RepairEngineError
from domoai.domain.connections import ConnectionDiagnostic, DiagnosticSeverity
from domoai.domain.repairs import RepairActionName
from domoai.runtime.clock import FixedClock


def _diagnostic(code: str, *, retryable: bool = False) -> ConnectionDiagnostic:
    return ConnectionDiagnostic(
        code=code,
        severity=DiagnosticSeverity.ERROR,
        retryable=retryable,
        message=f"diagnostic: {code}",
    )


class _Manager:
    def __init__(self, diagnostics: tuple[ConnectionDiagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        self.repaired: list[UUID] = []
        self.reauthenticated: list[str] = []
        self.reconfigured: list[dict[str, object]] = []

    async def diagnose(self, connection_id: UUID) -> tuple[ConnectionDiagnostic, ...]:
        return self.diagnostics

    async def repair(self, connection_id: UUID) -> str:
        self.repaired.append(connection_id)
        return "retried"

    async def reauth(self, connection_id: UUID, credential_value: str) -> str:
        self.reauthenticated.append(credential_value)
        return "reauthenticated"

    async def get(self, connection_id: UUID):
        raise AssertionError("configuration action was not expected")


@pytest.mark.asyncio
async def test_repair_plans_are_deterministic_and_catalog_known_codes() -> None:
    connection_id = uuid4()
    manager = _Manager(
        (
            _diagnostic("timeout", retryable=True),
            _diagnostic("authentication_failed"),
            _diagnostic("mapping_required"),
            _diagnostic("tls_certificate_invalid"),
        )
    )
    engine = RepairEngine(manager, clock=FixedClock(datetime(2026, 1, 1, tzinfo=UTC)))

    first = await engine.plan(connection_id)
    second = await engine.plan(connection_id)

    assert first.model_dump() == second.model_dump()
    assert {item.action for item in first.actions} == {
        RepairActionName.RETRY,
        RepairActionName.REAUTHENTICATE,
        RepairActionName.IMPORT_MAPPING,
        RepairActionName.TRUST_CERTIFICATE,
    }
    assert all(item.plan_digest for item in first.actions)


@pytest.mark.asyncio
async def test_retry_delegates_without_confirmation_but_mutation_does_not() -> None:
    connection_id = uuid4()
    manager = _Manager((_diagnostic("timeout", retryable=True),))
    engine = RepairEngine(manager)
    plan = await engine.suggest(connection_id)

    result = await engine.execute(plan, plan.actions[0].action_id)

    assert result == "retried"
    assert manager.repaired == [connection_id]

    manager.diagnostics = (_diagnostic("authentication_failed"),)
    mutation_plan = await engine.plan(connection_id)
    with pytest.raises(RepairEngineError, match="confirmation"):
        await engine.execute(mutation_plan, mutation_plan.actions[0].action_id)
