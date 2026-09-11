import asyncio
from pathlib import Path

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.domain.models import AuditEvent


@pytest.mark.composition
@pytest.mark.asyncio
async def test_runtime_supervises_and_cancels_audit_outbox_delivery(tmp_path: Path) -> None:
    delivered = asyncio.Event()
    delivered_ids: list[str] = []
    delivered_payloads: list[dict[str, object]] = []

    async def delivery(event: AuditEvent) -> None:
        delivered_ids.append(event.id)
        delivered_payloads.append(event.payload)
        if event.payload.get("marker") == "runtime-composition":
            delivered.set()

    runtime = await build_runtime(
        Settings(
            database_path=tmp_path / "runtime.sqlite3",
            audit_database_path=tmp_path / "runtime-audit.sqlite3",
            audit_outbox_poll_interval_seconds=0.01,
            audit_outbox_lease_seconds=5,
        ),
        adapter=SimulatedHomeAdapter(),
        audit_outbox_delivery=delivery,
    )
    event = runtime.audit.append(
        event_type="plan_execution_completed",
        actor="composition-test",
        subject_id="plan-composition",
        payload={"marker": "runtime-composition", "token": "secret"},
    )
    await runtime.start()
    task = runtime.lifecycle.task("domoai-audit-outbox-dispatcher")
    assert task is not None
    assert not task.done()

    await asyncio.wait_for(delivered.wait(), timeout=3)
    assert event.id in delivered_ids
    assert any(payload.get("marker") == "runtime-composition" for payload in delivered_payloads)
    assert all("secret" not in str(payload) for payload in delivered_payloads)

    await runtime.close()
    assert task.done()
