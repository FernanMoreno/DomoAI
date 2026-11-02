"""T004 fixture composition checks, not receiver-fence or resident-product acceptance."""

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from domoai.runtime.execution_context import ExecutionContext
from tests.integration.test_household_consent import INITIAL_TIME, consent_fixture

pytestmark = pytest.mark.composition


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t004_fixture_controls_simulated_write_ack_and_clock(
    tmp_path: Path, backend: str
) -> None:
    async with consent_fixture(tmp_path, backend) as fixture:
        home_a, home_b = fixture.homes
        adapter = home_a.adapter
        command = home_a.command()
        context = ExecutionContext(
            plan_id="t004-plan",
            execution_attempt_id="t004-attempt",
            adapter_request_id="t004-request",
            client_principal_id=home_a.agent.principal_id,
        )
        pending = asyncio.create_task(adapter.execute(command, context))
        try:
            await adapter.before_write.wait_until_reached()
            assert not pending.done() and adapter.calls == []
            assert home_b.adapter.calls == []
            assert not getattr(adapter, "supports_fencing", False)
            fixture.clock.set(INITIAL_TIME + timedelta(seconds=11))
            adapter.before_write.release()
            await adapter.before_ack.wait_until_reached()
            assert adapter.calls == [command] and adapter.contexts == [context]
            assert not pending.done() and home_b.adapter.calls == []
            sources = home_a.registry.get(command.device_id).source_refs
            states = await adapter.read_state(sources)
            assert states and all(state.observed_at == fixture.clock.now() for state in states)
            other_states = await home_b.adapter.read_state(sources)
            assert any(state.value is True for state in states if state.capability == "power")
            assert all(
                state.value is False for state in other_states if state.capability == "power"
            )
            adapter.before_ack.release()
            assert (await asyncio.wait_for(pending, timeout=5)).accepted
        finally:
            adapter.before_write.release()
            adapter.before_ack.release()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
