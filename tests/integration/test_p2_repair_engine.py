from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from domoai.application.repair_engine import RepairEngine
from domoai.domain.connections import ConnectionDiagnostic, DiagnosticSeverity


class _Manager:
    def __init__(self) -> None:
        self.diagnostics = (
            ConnectionDiagnostic(
                code="timeout",
                severity=DiagnosticSeverity.ERROR,
                retryable=True,
                message="timeout",
            ),
        )
        self.active = 0
        self.maximum_active = 0

    async def diagnose(self, _connection_id):
        return self.diagnostics

    async def repair(self, _connection_id):
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        return "ok"


@pytest.mark.asyncio
async def test_repairs_for_one_connection_are_serialized() -> None:
    manager = _Manager()
    engine = RepairEngine(manager)
    connection_id = uuid4()
    plan = await engine.plan(connection_id)

    results = await asyncio.gather(
        engine.execute(plan, plan.actions[0].action_id),
        engine.execute(plan, plan.actions[0].action_id),
    )

    assert results == ["ok", "ok"]
    assert manager.maximum_active == 1
