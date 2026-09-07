from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from domoai.domain.coordination import (
    FencingViolation,
    LeaseScope,
    LeaseUnavailable,
)


def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


class FakeEtcdJson:
    def __init__(self) -> None:
        self.kvs: dict[str, dict[str, str]] = {}
        self.leases: dict[str, set[str]] = {}
        self.next_lease = 41
        self.revision = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        if request.url.path == "/v3/lease/grant":
            lease_id = str(self.next_lease)
            self.next_lease += 1
            self.leases[lease_id] = set()
            return httpx.Response(200, json={"ID": lease_id, "TTL": body["TTL"]})
        if request.url.path == "/v3/lease/keepalive":
            lease_id = str(body["ID"])
            if lease_id not in self.leases:
                return httpx.Response(200, json={"ID": lease_id, "TTL": 0})
            return httpx.Response(200, json={"ID": lease_id, "TTL": 30})
        if request.url.path == "/v3/lease/revoke":
            lease_id = str(body["ID"])
            for key in self.leases.pop(lease_id, set()):
                self.kvs.pop(key, None)
            return httpx.Response(200, json={"header": {"revision": str(self.revision)}})
        if request.url.path == "/v3/kv/range":
            key = base64.b64decode(body["key"]).decode()
            value = self.kvs.get(key)
            return httpx.Response(
                200,
                json={"kvs": [{"key": _b64(key), **value}] if value else []},
            )
        if request.url.path == "/v3/kv/txn":
            return self._txn(body)
        return httpx.Response(404)

    def _txn(self, body: dict[str, Any]) -> httpx.Response:
        compares = body.get("compare", [])
        success = True
        for compare in compares:
            key = base64.b64decode(compare["key"]).decode()
            value = self.kvs.get(key)
            target = compare["target"]
            if target == "CREATE":
                expected = str(compare["create_revision"])
                actual = value["create_revision"] if value else "0"
            elif target == "MOD":
                expected = str(compare["mod_revision"])
                actual = value["mod_revision"] if value else "0"
            elif target == "VALUE":
                expected = base64.b64decode(compare["value"]).decode()
                actual = value["value"] if value else ""
            else:  # pragma: no cover - the provider should never send this.
                raise AssertionError(target)
            success = success and actual == expected
        for operation in body.get("success" if success else "failure", []):
            put = operation.get("request_put")
            if put is None:
                continue
            key = base64.b64decode(put["key"]).decode()
            self.revision += 1
            old = self.kvs.get(key)
            value = base64.b64decode(put["value"]).decode()
            entry = {
                "value": put["value"],
                "create_revision": old["create_revision"] if old else str(self.revision),
                "mod_revision": str(self.revision),
            }
            self.kvs[key] = entry
            if "lease" in put:
                lease_id = str(put["lease"])
                self.leases[lease_id].add(key)
        return httpx.Response(200, json={"succeeded": success, "responses": []})

    def expire(self, lease_id: str) -> None:
        for key in self.leases.pop(lease_id, set()):
            self.kvs.pop(key, None)


def _client(fake: FakeEtcdJson) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="https://etcd.test",
        transport=fake.transport(),
    )


@pytest.fixture
def scope() -> LeaseScope:
    return LeaseScope(tenant_id="tenant", household_id="home", deployment_id="edge")


@pytest.mark.asyncio
async def test_coordinator_closes_a_client_it_owns() -> None:
    from domoai.application.etcd_coordination import EtcdHttpLeaseCoordinator

    client = httpx.AsyncClient()
    coordinator = EtcdHttpLeaseCoordinator(
        ("http://etcd.test",), client=client, owns_client=True
    )

    await coordinator.aclose()

    assert client.is_closed


@pytest.mark.asyncio
async def test_etcd_acquire_uses_cas_and_monotonic_epoch(scope: LeaseScope) -> None:
    from domoai.application.etcd_coordination import EtcdHttpLeaseCoordinator

    fake = FakeEtcdJson()
    client = _client(fake)
    coordinator = EtcdHttpLeaseCoordinator(("https://etcd.test",), client=client)

    first = await coordinator.acquire(scope, owner_id="host-a", ttl_seconds=30)
    await coordinator.release(first)
    second = await coordinator.acquire(scope, owner_id="host-b", ttl_seconds=30)

    assert first.epoch == 1
    assert second.epoch == 2
    assert second.owner_id == "host-b"

    await client.aclose()


@pytest.mark.asyncio
async def test_etcd_rejects_token_after_lease_expiry_and_takeover(scope: LeaseScope) -> None:
    from domoai.application.etcd_coordination import EtcdHttpLeaseCoordinator

    fake = FakeEtcdJson()
    client = _client(fake)
    coordinator = EtcdHttpLeaseCoordinator(("https://etcd.test",), client=client)
    first = await coordinator.acquire(scope, owner_id="host-a", ttl_seconds=30)
    fake.expire(first.lease_id)
    second = await coordinator.acquire(scope, owner_id="host-b", ttl_seconds=30)

    with pytest.raises(FencingViolation):
        await coordinator.validate(first)
    await coordinator.validate(second)
    await client.aclose()


@pytest.mark.asyncio
async def test_etcd_renew_and_release_require_current_token(scope: LeaseScope) -> None:
    from domoai.application.etcd_coordination import EtcdHttpLeaseCoordinator

    fake = FakeEtcdJson()
    client = _client(fake)
    coordinator = EtcdHttpLeaseCoordinator(("https://etcd.test",), client=client)
    token = await coordinator.acquire(scope, owner_id="host-a", ttl_seconds=30)
    renewed = await coordinator.renew(token)
    assert renewed.epoch == token.epoch
    await coordinator.release(renewed)
    with pytest.raises(FencingViolation):
        await coordinator.validate(renewed)
    await client.aclose()


@pytest.mark.asyncio
async def test_etcd_connection_failure_is_fail_closed(scope: LeaseScope) -> None:
    from domoai.application.etcd_coordination import EtcdHttpLeaseCoordinator

    def fail(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("partitioned", request=_)

    client = httpx.AsyncClient(
        base_url="https://etcd.test",
        transport=httpx.MockTransport(fail),
    )
    coordinator = EtcdHttpLeaseCoordinator(("https://etcd.test",), client=client)

    with pytest.raises(LeaseUnavailable):
        await coordinator.acquire(scope, owner_id="host-a", ttl_seconds=30)
    await client.aclose()
