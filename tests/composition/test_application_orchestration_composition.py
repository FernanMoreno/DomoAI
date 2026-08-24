"""Application orchestration composes only through lower-level boundaries."""

from __future__ import annotations


def test_application_orchestration_modules_share_application_ownership() -> None:
    from domoai.application.event_consumer import RuntimeEventConsumer
    from domoai.application.executor import PlanExecutor
    from domoai.application.policy_engine import PolicyEngine
    from domoai.application.scheduler import Scheduler

    assert {
        RuntimeEventConsumer.__module__.split(".")[1],
        PlanExecutor.__module__.split(".")[1],
        PolicyEngine.__module__.split(".")[1],
        Scheduler.__module__.split(".")[1],
    } == {"application"}
