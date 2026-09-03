from typing import cast

from domoai.application.execution_admission import ExecutionAdmission
from domoai.application.executor import PlanExecutor
from domoai.application.facade import DomoticsFacade
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.runtime.events import AuditLog
from domoai.runtime.ports import AdapterPort
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


def test_facade_exposes_executor_execution_admission_instance() -> None:
    admission = ExecutionAdmission()
    plan_service = PlanService(DeviceRegistry(), StateStore(), PolicyEngine([]), AuditLog())
    executor = PlanExecutor(
        cast(AdapterPort, object()),
        plan_service,
        AuditLog(),
        execution_admission=admission,
    )

    facade = DomoticsFacade(plan_service, executor)

    assert facade.execution_admission is admission
