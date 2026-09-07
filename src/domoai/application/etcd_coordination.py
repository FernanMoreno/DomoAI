"""etcd v3 JSON-gateway implementation of the lease coordinator contract."""

from __future__ import annotations

import base64
import hashlib
import json
import ssl
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

from domoai.config.settings import Settings
from domoai.domain.coordination import (
    FencingToken,
    FencingViolation,
    LeaseCoordinator,
    LeaseScope,
    LeaseUnavailable,
)
from domoai.runtime.clock import Clock, SystemClock


def _encoded(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _decoded(value: str) -> str:
    return base64.b64decode(value.encode("ascii")).decode("utf-8")


def _result(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    result = payload.get("result")
    return result if isinstance(result, Mapping) else payload


class EtcdHttpLeaseCoordinator(LeaseCoordinator):
    """Coordinate physical ownership through etcd's v3 JSON API.

    The JSON gateway is used deliberately instead of depending on an
    unmaintained Python etcd client.  The caller must provide an HTTPS client
    configured with CA/client certificates for production use.  Tests may
    inject an ``httpx.AsyncClient`` with a ``MockTransport``.
    """

    def __init__(
        self,
        endpoints: Sequence[str],
        *,
        client: httpx.AsyncClient | None = None,
        owns_client: bool = False,
        clock: Clock | None = None,
        namespace: str = "domoai/coordination/v1",
        request_timeout_seconds: float = 5.0,
        max_acquire_attempts: int = 8,
    ) -> None:
        normalized = tuple(endpoint.rstrip("/") for endpoint in endpoints if endpoint.strip())
        if not normalized:
            raise ValueError("at least one etcd endpoint is required")
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if max_acquire_attempts < 1:
            raise ValueError("max_acquire_attempts must be positive")
        self.endpoints = normalized
        self.clock = clock or SystemClock()
        self.namespace = namespace.strip("/")
        self.request_timeout = httpx.Timeout(request_timeout_seconds)
        self.max_acquire_attempts = max_acquire_attempts
        self._client = client
        self._owns_client = client is None or owns_client

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.request_timeout)
        return self._client

    async def _request(self, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        client = await self._ensure_client()
        last_error: BaseException | None = None
        for endpoint in self.endpoints:
            try:
                response = await client.post(
                    f"{endpoint}{path}",
                    json=dict(body),
                    timeout=self.request_timeout,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, Mapping):
                    raise RuntimeError("etcd returned a non-object response")
                if payload.get("error"):
                    raise RuntimeError("etcd returned an error")
                return payload
            except (httpx.HTTPError, ValueError, RuntimeError) as error:
                last_error = error
        raise LeaseUnavailable("etcd coordination request failed") from last_error

    @staticmethod
    def _scope_digest(scope: LeaseScope) -> str:
        serialized = json.dumps(
            scope.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    def _keys(self, scope: LeaseScope) -> tuple[str, str]:
        prefix = f"{self.namespace}/{self._scope_digest(scope)}"
        return f"{prefix}/epoch", f"{prefix}/lease"

    async def _range(self, key: str) -> dict[str, str] | None:
        payload = await self._request("/v3/kv/range", {"key": _encoded(key)})
        kvs = _result(payload).get("kvs", payload.get("kvs", []))
        if not isinstance(kvs, list) or not kvs:
            return None
        first = kvs[0]
        if not isinstance(first, Mapping):
            raise LeaseUnavailable("etcd returned an invalid key-value record")
        return {str(key_name): str(value) for key_name, value in first.items()}

    async def _grant(self, ttl_seconds: float) -> tuple[str, float]:
        payload = await self._request("/v3/lease/grant", {"TTL": int(ttl_seconds)})
        response = _result(payload)
        try:
            lease_id = str(response["ID"])
            ttl = float(response.get("TTL", ttl_seconds))
        except (KeyError, TypeError, ValueError) as error:
            raise LeaseUnavailable("etcd returned an invalid lease") from error
        if not lease_id or ttl <= 0:
            raise LeaseUnavailable("etcd returned an unusable lease")
        return lease_id, ttl

    async def _keepalive(self, lease_id: str) -> float:
        payload = await self._request("/v3/lease/keepalive", {"ID": lease_id})
        response = _result(payload)
        try:
            ttl = float(response.get("TTL", 0))
        except (TypeError, ValueError) as error:
            raise LeaseUnavailable("etcd returned an invalid keepalive") from error
        if ttl <= 0:
            raise FencingViolation("etcd lease has expired")
        return ttl

    async def _revoke(self, lease_id: str) -> None:
        await self._request("/v3/lease/revoke", {"ID": lease_id})

    async def _transaction(self, body: Mapping[str, Any]) -> bool:
        payload = await self._request("/v3/kv/txn", body)
        response = _result(payload)
        return bool(response.get("succeeded", payload.get("succeeded", False)))

    @staticmethod
    def _compare(key: str, *, target: str, value: str) -> dict[str, str]:
        return {
            "key": _encoded(key),
            "target": target,
            "result": "EQUAL",
            {"CREATE": "create_revision", "MOD": "mod_revision", "VALUE": "value"}[target]: (
                _encoded(value) if target == "VALUE" else value
            ),
        }

    @staticmethod
    def _record_value(record: Mapping[str, str]) -> str:
        try:
            return _decoded(record["value"])
        except (KeyError, ValueError) as error:
            raise LeaseUnavailable("etcd returned an invalid value") from error

    def _assert_record_matches(
        self,
        token: FencingToken,
        record: Mapping[str, str] | None,
        *,
        check_local_expiry: bool,
    ) -> None:
        if record is None:
            raise FencingViolation("lease is unknown")
        try:
            current = json.loads(self._record_value(record))
        except (json.JSONDecodeError, TypeError) as error:
            raise FencingViolation("lease record is invalid") from error
        if not isinstance(current, Mapping):
            raise FencingViolation("lease record is invalid")
        if (
            current.get("owner_id") != token.owner_id
            or str(current.get("lease_id")) != token.lease_id
            or int(current.get("epoch", 0)) != token.epoch
            or current.get("scope") != token.scope.model_dump(mode="json")
        ):
            raise FencingViolation("stale fencing token")
        if check_local_expiry and token.expires_at <= self.clock.now():
            raise FencingViolation("lease is expired")

    async def acquire(
        self, scope: LeaseScope, *, owner_id: str, ttl_seconds: float
    ) -> FencingToken:
        if not owner_id.strip():
            raise ValueError("lease owner_id must be non-empty")
        if ttl_seconds <= 1:
            raise ValueError("lease TTL must be greater than one second")
        epoch_key, lease_key = self._keys(scope)
        lease_id, server_ttl = await self._grant(ttl_seconds)
        try:
            for _ in range(self.max_acquire_attempts):
                epoch_record = await self._range(epoch_key)
                current_epoch = int(self._record_value(epoch_record)) if epoch_record else 0
                epoch_compare = (
                    self._compare(epoch_key, target="MOD", value=epoch_record["mod_revision"])
                    if epoch_record is not None
                    else self._compare(epoch_key, target="CREATE", value="0")
                )
                now = self.clock.now()
                value = json.dumps(
                    {
                        "scope": scope.model_dump(mode="json"),
                        "owner_id": owner_id,
                        "epoch": current_epoch + 1,
                        "lease_id": lease_id,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                succeeded = await self._transaction(
                    {
                        "compare": [
                            self._compare(lease_key, target="CREATE", value="0"),
                            epoch_compare,
                        ],
                        "success": [
                            {
                                "request_put": {
                                    "key": _encoded(epoch_key),
                                    "value": _encoded(str(current_epoch + 1)),
                                }
                            },
                            {
                                "request_put": {
                                    "key": _encoded(lease_key),
                                    "value": _encoded(value),
                                    "lease": lease_id,
                                }
                            },
                        ],
                        "failure": [],
                    }
                )
                if succeeded:
                    return FencingToken(
                        scope=scope,
                        owner_id=owner_id,
                        epoch=current_epoch + 1,
                        lease_id=lease_id,
                        issued_at=now,
                        expires_at=now + timedelta(seconds=server_ttl),
                    )
                current = await self._range(lease_key)
                if current is not None:
                    raise LeaseUnavailable("lease scope is owned by another host")
            raise LeaseUnavailable("etcd lease acquisition conflicted repeatedly")
        except BaseException:
            await self._revoke(lease_id)
            raise

    async def renew(self, token: FencingToken) -> FencingToken:
        record = await self._range(self._keys(token.scope)[1])
        self._assert_record_matches(token, record, check_local_expiry=False)
        ttl = await self._keepalive(token.lease_id)
        record = await self._range(self._keys(token.scope)[1])
        self._assert_record_matches(token, record, check_local_expiry=False)
        now = self.clock.now()
        return token.model_copy(
            update={"issued_at": now, "expires_at": now + timedelta(seconds=ttl)}
        )

    async def release(self, token: FencingToken) -> None:
        await self.validate(token)
        await self._revoke(token.lease_id)

    async def validate(self, token: FencingToken) -> None:
        record = await self._range(self._keys(token.scope)[1])
        self._assert_record_matches(token, record, check_local_expiry=True)

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


def build_external_etcd_http_client(settings: Settings) -> httpx.AsyncClient:
    """Build the mTLS HTTP client shared by etcd coordination checks."""

    if not settings.etcd_endpoints:
        raise ValueError("multi-host runtime requires etcd endpoints")
    if any(
        (parsed := urlparse(endpoint)).scheme != "https" or not parsed.netloc
        for endpoint in settings.etcd_endpoints
    ):
        raise ValueError("multi-host runtime requires HTTPS etcd endpoints")
    ca_path = settings.etcd_ca_cert_path
    client_cert_path = settings.etcd_client_cert_path
    client_key_path = settings.etcd_client_key_path
    if any(path is None for path in (ca_path, client_cert_path, client_key_path)):
        raise ValueError("multi-host runtime requires etcd mTLS certificates")
    assert ca_path is not None
    assert client_cert_path is not None
    assert client_key_path is not None
    if any(
        path.is_symlink() or not path.is_file()
        for path in (ca_path, client_cert_path, client_key_path)
    ):
        raise ValueError("multi-host runtime requires regular etcd mTLS certificate files")
    tls = ssl.create_default_context(cafile=str(ca_path))
    tls.load_cert_chain(certfile=str(client_cert_path), keyfile=str(client_key_path))
    return httpx.AsyncClient(
        verify=tls,
        timeout=httpx.Timeout(settings.etcd_request_timeout_seconds),
    )


def build_external_lease_coordinator(settings: Settings) -> EtcdHttpLeaseCoordinator:
    """Build the production etcd provider only from complete mTLS settings."""

    return EtcdHttpLeaseCoordinator(
        settings.etcd_endpoints,
        client=build_external_etcd_http_client(settings),
        owns_client=True,
        request_timeout_seconds=settings.etcd_request_timeout_seconds,
    )
