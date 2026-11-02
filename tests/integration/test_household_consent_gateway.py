"""T012-C6 RED: the built gateway serves the resident consent flow over real runtime data."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from domoai.domain.models import PrincipalRole
from domoai.mcp.token_lifecycle import TokenFileManager

ORIGIN = "http://127.0.0.1:8765"


def _provision(tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "resident.json"
    token = (
        TokenFileManager(path)
        .rotate(
            "resident",
            scopes=["consent:human"],
            tenant_id="tenant-a",
            household_ids=["home-a"],
            roles=[PrincipalRole.OWNER],
            operations=["review_consent", "approve_consent", "revoke_consent"],
        )
        .token
    )
    return {"resident_token_file": path, "resident_origin": ORIGIN, "token": token}


def _overrides(files: dict[str, Any]) -> dict[str, Any]:
    return {key: files[key] for key in ("resident_token_file", "resident_origin")}


async def _seed(gateway: Any, plan_id: str = "plan-a", **plan_changes: Any) -> None:
    from domoai.domain.models import Plan
    from tests.security.test_household_consent_security import _c3_device, _c3_plan_dict

    runtime = gateway.runtime
    runtime.registry.load_persisted([_c3_device("light-a")])
    data = _c3_plan_dict(runtime.clock.now(), plan_id, **plan_changes)
    data["deployment_id"] = "edge"
    await runtime.plan_repository.save(Plan.model_validate(data))


class _Flow:
    def __init__(self, client: Any, files: dict[str, Any]) -> None:
        self.client, self.files, self.session_id = client, files, ""

    def headers(self, **changes: Any) -> dict[str, str]:
        headers = {
            "Authorization": "Bearer " + self.files["token"],
            "Origin": ORIGIN,
            "X-Resident-Session": self.session_id,
            "Content-Type": "application/json",
        }
        headers.update(changes)
        return {key: value for key, value in headers.items() if value is not None}

    async def open(self) -> None:
        created = await self.client.post(
            "/api/v1/resident/session",
            headers={"Authorization": "Bearer " + self.files["token"], "Origin": ORIGIN},
        )
        assert created.status_code == 201, created.text
        self.session_id = created.json()["session_id"]

    async def review(self, plan_id: str = "plan-a", kind: str = "plan") -> Any:
        return await self.client.post(
            "/api/v1/resident/reviews",
            headers=self.headers(),
            content=json.dumps({"reference": {"kind": kind, "id": plan_id}}),
        )

    async def decide(
        self, payload: dict[str, Any], request_id: str = "req-1", action: str = "approve"
    ) -> Any:
        return await self.client.post(
            "/api/v1/resident/decisions",
            headers=self.headers(),
            content=json.dumps(
                {
                    "challenge_id": payload["challenge_id"],
                    "action": action,
                    "expected_review_digest": payload["review_digest"],
                    "request_id": request_id,
                }
            ),
        )

    async def receipt(self, request_id: str = "req-1") -> Any:
        return await self.client.get(
            f"/api/v1/resident/decisions/{request_id}",
            headers=self.headers(**{"Content-Type": None}),
        )


async def test_t012_c6_gateway_serves_review_approve_and_receipt_over_real_data(
    tmp_path: Path,
) -> None:
    from tests.fixtures.household_onboarding import onboarding_gateway

    files = _provision(tmp_path)
    async with onboarding_gateway(tmp_path, **_overrides(files)) as gateway:
        await _seed(gateway)
        async with gateway.http_client() as client:
            flow = _Flow(client, files)
            await flow.open()
            reviewed = await flow.review()
            assert reviewed.status_code == 200, reviewed.text
            payload = reviewed.json()
            head = gateway.runtime.approval_store.get_consent_control("tenant-a", "home-a")
            assert payload["view"]["bindings"]["control_version"] == head.model_dump(mode="json")
            approved = await flow.decide(payload)
            assert approved.status_code == 200, approved.text
            assert approved.json()["status"] == "approved"
            receipt = await flow.receipt()
            assert receipt.status_code == 200 and receipt.content == approved.content
            assert gateway.runtime.approval_store.durable is True


async def test_t012_c6_receipt_survives_a_gateway_restart(tmp_path: Path) -> None:
    from tests.fixtures.household_onboarding import onboarding_gateway

    files = _provision(tmp_path)
    async with onboarding_gateway(tmp_path, **_overrides(files)) as gateway:
        await _seed(gateway)
        async with gateway.http_client() as client:
            flow = _Flow(client, files)
            await flow.open()
            payload = (await flow.review()).json()
            first = await flow.decide(payload)
            assert first.status_code == 200, first.text
        head = gateway.runtime.approval_store.get_consent_control("tenant-a", "home-a")
    async with onboarding_gateway(tmp_path, **_overrides(files)) as gateway:
        assert gateway.runtime.approval_store.get_consent_control("tenant-a", "home-a") == head
        async with gateway.http_client() as client:
            flow = _Flow(client, files)
            await flow.open()
            receipt = await flow.receipt()
            assert receipt.status_code == 200 and receipt.content == first.content
            retry = await flow.decide(payload)
            assert retry.status_code == 200 and retry.content == first.content


@pytest.mark.parametrize("target", ["missing-plan", "foreign-plan"])
async def test_t012_c6_unknown_or_foreign_targets_are_a_generic_404_not_a_503(
    tmp_path: Path, target: str
) -> None:
    from tests.fixtures.household_onboarding import onboarding_gateway

    files = _provision(tmp_path)
    async with onboarding_gateway(tmp_path, **_overrides(files)) as gateway:
        await _seed(gateway)
        await _seed(gateway, "foreign-plan", tenant="tenant-b", household="home-b")
        async with gateway.http_client() as client:
            flow = _Flow(client, files)
            await flow.open()
            response = await flow.review(target)
            assert response.status_code == 404, response.text


async def test_t012_c6_an_absent_rule_is_a_generic_404(tmp_path: Path) -> None:
    from tests.fixtures.household_onboarding import onboarding_gateway

    files = _provision(tmp_path)
    async with onboarding_gateway(tmp_path, **_overrides(files)) as gateway:
        await _seed(gateway)
        async with gateway.http_client() as client:
            flow = _Flow(client, files)
            await flow.open()
            response = await flow.review("rule-absent", kind="rule")
            assert response.status_code == 404, response.text


async def test_t012_c6_a_changed_registry_between_review_and_decide_stops_the_decision(
    tmp_path: Path,
) -> None:
    from tests.fixtures.household_onboarding import onboarding_gateway
    from tests.security.test_household_consent_security import _c3_device

    files = _provision(tmp_path)
    async with onboarding_gateway(tmp_path, **_overrides(files)) as gateway:
        await _seed(gateway)
        async with gateway.http_client() as client:
            flow = _Flow(client, files)
            await flow.open()
            payload = (await flow.review()).json()
            changed = _c3_device("light-a").model_copy(update={"name": "Otra luz"})
            gateway.runtime.registry.load_persisted([changed])
            response = await flow.decide(payload)
            assert response.status_code == 409, response.text
            assert response.json()["error"] == "challenge_invalid"


async def test_t012_c6_store_exposes_durability_and_bootstraps_the_control_head(
    tmp_path: Path,
) -> None:
    from domoai.domain.errors import DomainError, ErrorCode
    from domoai.runtime.approval_store import ApprovalStore
    from tests.fixtures.household_onboarding import onboarding_gateway

    files = _provision(tmp_path)
    async with onboarding_gateway(tmp_path, **_overrides(files)) as gateway:
        store = gateway.runtime.approval_store
        assert store.durable is True
        first = store.get_consent_control("tenant-a", "home-a")
        assert store.get_consent_control("tenant-a", "home-a") == first
    memory = ApprovalStore()
    assert memory.durable is False
    with pytest.raises(DomainError) as caught:
        memory.get_consent_control("tenant-a", "home-a")
    assert caught.value.code == ErrorCode.SOURCE_UNAVAILABLE


async def test_t012_c6_review_source_fails_closed_on_repository_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fixtures.household_onboarding import onboarding_gateway

    files = _provision(tmp_path)
    async with onboarding_gateway(tmp_path, **_overrides(files)) as gateway:
        await _seed(gateway)

        calls: list[Any] = []

        async def boom(*args: Any, **__: Any) -> Any:
            calls.append(args)
            raise RuntimeError("secret internal failure")

        monkeypatch.setattr(gateway.runtime.household_preferences, "read", boom)
        async with gateway.http_client() as client:
            flow = _Flow(client, files)
            await flow.open()
            response = await flow.review()
            assert response.status_code == 404
            assert "secret" not in response.text
            assert len(calls) == 1, "the real source must read preferences exactly once"


async def test_t012_c6_a_non_durable_store_keeps_review_and_decisions_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from domoai.runtime.approval_store import ApprovalStore
    from tests.fixtures.household_onboarding import onboarding_gateway

    monkeypatch.setattr(ApprovalStore, "durable", property(lambda self: False), raising=False)
    files = _provision(tmp_path)
    async with onboarding_gateway(tmp_path, **_overrides(files)) as gateway:
        await _seed(gateway)
        async with gateway.http_client() as client:
            flow = _Flow(client, files)
            await flow.open()
            reviewed = await flow.review()
            assert reviewed.status_code == 503, reviewed.text
            assert reviewed.json()["error"] == "human_confirmation_unavailable"
            decided = await flow.decide(
                {"challenge_id": "c" * 43, "review_digest": "sha256:" + "0" * 64}
            )
            assert decided.status_code == 503, decided.text


async def test_t012_c6_a_persisted_rule_is_reviewed_but_cannot_be_decided(
    tmp_path: Path,
) -> None:
    from datetime import timedelta

    from domoai.domain.automation import AutomationConsent
    from domoai.domain.models import AuthorityContext
    from tests.fixtures.household_onboarding import onboarding_gateway
    from tests.security.test_household_consent_security import _c3_device, _c3_rule

    files = _provision(tmp_path)
    async with onboarding_gateway(tmp_path, **_overrides(files)) as gateway:
        runtime = gateway.runtime
        runtime.registry.load_persisted([_c3_device("light-a")])
        now = runtime.clock.now()
        rule = _c3_rule(now)
        consent = AutomationConsent(
            authority=AuthorityContext(
                tenant_id="tenant-a",
                household_id="home-a",
                household_ids=["home-a"],
                principal_id="planner-a",
                roles=["planner"],
            ),
            approval_id="approval-rule",
            principal_id="planner-a",
            scope=rule.scope,
            rule_digest=rule.definition_digest,
            approved_at=now,
            expires_at=now + timedelta(hours=1),
        )
        await runtime.automation_rule_repository.save(rule, consent)
        async with gateway.http_client() as client:
            flow = _Flow(client, files)
            await flow.open()
            reviewed = await flow.review("rule-a", kind="rule")
            assert reviewed.status_code == 200, reviewed.text
            assert reviewed.json()["view"]["reference"]["kind"] == "rule"
            decided = await flow.decide(reviewed.json())
            assert decided.status_code == 409 and decided.json()["error"] == "decision_unsupported"


async def test_t011_c7_the_built_gateway_revokes_an_approval_and_keeps_the_receipt(
    tmp_path: Path,
) -> None:
    from domoai.domain.errors import DomainError, ErrorCode
    from tests.fixtures.household_onboarding import onboarding_gateway

    files = _provision(tmp_path)
    async with onboarding_gateway(tmp_path, **_overrides(files)) as gateway:
        await _seed(gateway)
        async with gateway.http_client() as client:
            flow = _Flow(client, files)
            await flow.open()
            approved = await flow.decide((await flow.review()).json())
            assert approved.status_code == 200, approved.text
            payload = (await flow.review()).json()
            revoked = await flow.decide(payload, "rev-1", "revoke")
            assert revoked.status_code == 200, revoked.text
            body = revoked.json()
            assert body["status"] == "revocation_pending"
            assert body["approval_id"] == approved.json()["approval_id"]
            with pytest.raises(DomainError) as caught:
                gateway.runtime.approval_store._persistence.assert_consent_active_sync(
                    body["approval_id"]
                )
            assert caught.value.code == ErrorCode.CONSENT_REVOKED
            assert (await flow.receipt("rev-1")).content == revoked.content
    async with onboarding_gateway(tmp_path, **_overrides(files)) as gateway:
        async with gateway.http_client() as client:
            flow = _Flow(client, files)
            await flow.open()
            assert (await flow.receipt("rev-1")).content == revoked.content
            retry = await flow.decide(payload, "rev-1", "revoke")
            assert retry.status_code == 200 and retry.content == revoked.content
