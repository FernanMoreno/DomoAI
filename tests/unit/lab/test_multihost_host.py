import pytest
from pydantic import ValidationError

from domoai.lab.multihost_host import HostRequest


def test_host_request_allows_only_allowlisted_actions() -> None:
    request = HostRequest.model_validate(
        {"action": "status", "household_id": "lab-household"}
    )

    assert request.action == "status"
    assert request.household_id == "lab-household"


def test_host_request_rejects_unknown_actions_and_fields() -> None:
    with pytest.raises(ValidationError):
        HostRequest.model_validate({"action": "shell"})

    with pytest.raises(ValidationError):
        HostRequest.model_validate({"action": "status", "password": "secret"})


def test_physical_intent_request_requires_idempotency_key() -> None:
    with pytest.raises(ValidationError):
        HostRequest.model_validate({"action": "physical_intent"})
