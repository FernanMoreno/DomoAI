from __future__ import annotations

import pytest
from pydantic import ValidationError

from domoai.domain.models import Command, ExecutionOutcome, ExecutionStatus, Plan


def _command() -> Command:
    return Command(
        id="cmd-1",
        device_id="light.kitchen",
        command="turn_on",
        idempotency_key="idem-1",
    )


def test_plan_defaults_agent_request_id_to_none() -> None:
    plan = Plan(id="plan-1", commands=[_command()])

    assert plan.agent_request_id is None


def test_plan_accepts_explicit_agent_request_id() -> None:
    plan = Plan(id="plan-1", commands=[_command()], agent_request_id="agent-req-1")

    assert plan.agent_request_id == "agent-req-1"


def test_execution_outcome_requires_execution_attempt_id() -> None:
    with pytest.raises(ValidationError):
        ExecutionOutcome(  # type: ignore[call-arg]
            plan_id="plan-1",
            command_id="cmd-1",
            status=ExecutionStatus.CONFIRMED_SUCCESS,
        )


def test_execution_outcome_defaults_adapter_request_id_to_none() -> None:
    outcome = ExecutionOutcome(
        plan_id="plan-1",
        command_id="cmd-1",
        execution_attempt_id="attempt-1",
        status=ExecutionStatus.CONFIRMED_SUCCESS,
    )

    assert outcome.adapter_request_id is None


def test_execution_outcome_accepts_explicit_adapter_request_id() -> None:
    outcome = ExecutionOutcome(
        plan_id="plan-1",
        command_id="cmd-1",
        execution_attempt_id="attempt-1",
        adapter_request_id="adapter-req-1",
        status=ExecutionStatus.CONFIRMED_SUCCESS,
    )

    assert outcome.adapter_request_id == "adapter-req-1"
