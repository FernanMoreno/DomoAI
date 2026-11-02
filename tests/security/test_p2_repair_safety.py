from __future__ import annotations

from uuid import uuid4

import pytest

from domoai.application.repair_engine import RepairEngine, RepairEngineError
from domoai.domain.connections import ConnectionDiagnostic, DiagnosticSeverity


class _Manager:
    def __init__(self) -> None:
        self.diagnostics = (
            ConnectionDiagnostic(
                code="authentication_failed",
                severity=DiagnosticSeverity.ERROR,
                retryable=False,
                message="authentication failed",
            ),
        )
        self.calls: list[str] = []

    async def diagnose(self, _connection_id):
        return self.diagnostics

    async def reauth(self, _connection_id, credential_value):
        self.calls.append(credential_value)
        return "ok"


@pytest.mark.asyncio
async def test_repair_rejects_stale_plans_and_secret_parameters() -> None:
    manager = _Manager()
    engine = RepairEngine(manager)
    plan = await engine.plan(uuid4())

    manager.diagnostics = (
        ConnectionDiagnostic(
            code="timeout",
            severity=DiagnosticSeverity.ERROR,
            retryable=True,
            message="timeout",
        ),
    )
    with pytest.raises(RepairEngineError, match="stale"):
        await engine.execute(plan, plan.actions[0].action_id, confirmed=True)

    manager.diagnostics = (
        ConnectionDiagnostic(
            code="authentication_failed",
            severity=DiagnosticSeverity.ERROR,
            retryable=False,
            message="authentication failed",
        ),
    )
    plan = await engine.plan(uuid4())
    with pytest.raises(RepairEngineError, match="sensitive"):
        await engine.execute(
            plan,
            plan.actions[0].action_id,
            confirmed=True,
            parameters={"password": "should-not-be-accepted"},
            credential="new-secret",
        )

    with pytest.raises(RepairEngineError, match="credential"):
        await engine.execute(plan, plan.actions[0].action_id, confirmed=True)
