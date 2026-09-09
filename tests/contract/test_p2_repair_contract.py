from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from domoai.application.repair_engine import RepairEngine
from domoai.domain.connections import ConnectionDiagnostic, DiagnosticSeverity
from domoai.management.api import create_management_app
from domoai.runtime.clock import FixedClock


class _Manager:
    async def diagnose(self, _connection_id):
        return (
            ConnectionDiagnostic(
                code="timeout",
                severity=DiagnosticSeverity.ERROR,
                retryable=True,
                message="timed out",
            ),
        )

    async def repair(self, _connection_id):
        return {"status": "retried"}


@pytest.mark.asyncio
async def test_repair_routes_return_typed_plan_and_delegate_execution() -> None:
    manager = _Manager()
    engine = RepairEngine(manager, clock=FixedClock(datetime(2026, 1, 1, tzinfo=UTC)))
    app = create_management_app(manager, repair_engine=engine)
    connection_id = uuid4()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        planned = await client.get(f"/api/v1/connections/{connection_id}/repairs")
        assert planned.status_code == 200
        payload = planned.json()
        assert payload["actions"][0]["action"] == "retry"

        executed = await client.post(
            f"/api/v1/connections/{connection_id}/repairs/{payload['actions'][0]['action_id']}",
            json={"plan": payload, "confirmed": False},
        )

    assert executed.status_code == 200
    assert executed.json() == {"status": "retried"}
