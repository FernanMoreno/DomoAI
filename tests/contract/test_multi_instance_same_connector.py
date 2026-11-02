from __future__ import annotations

from uuid import UUID, uuid4

from domoai.domain.models import SourceRef
from domoai.runtime.identity import ConnectionRuntimeIdentity, runtime_adapter_id


def test_same_connector_instances_have_distinct_stable_runtime_and_source_ids() -> None:
    first_id = uuid4()
    second_id = uuid4()
    first = ConnectionRuntimeIdentity("home_assistant", first_id)
    second = ConnectionRuntimeIdentity("home_assistant", second_id)

    assert first.connector_id == second.connector_id
    assert first.adapter_id == runtime_adapter_id(UUID(str(first_id)))
    assert first.adapter_id != second.adapter_id
    assert first.source_ref("light.kitchen") != second.source_ref("light.kitchen")
    assert isinstance(first.source_ref("light.kitchen"), SourceRef)


def test_runtime_id_does_not_depend_on_endpoint_or_credential() -> None:
    identity = ConnectionRuntimeIdentity("knx", UUID("12345678-1234-5678-1234-567812345678"))

    assert identity.adapter_id == "connection:12345678-1234-5678-1234-567812345678"
