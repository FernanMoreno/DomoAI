"""Safe, allowlisted subset of OpenAPI used by REST connector recipes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import yaml  # type: ignore[import-untyped]
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

_ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH"})
_PATH_ITEM_METADATA = frozenset({"parameters", "summary", "description", "servers", "$ref"})
_MAX_DOCUMENT_BYTES = 512 * 1024
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_REFERENCE_KEYS = frozenset({"$ref", "$dynamicRef", "$recursiveRef"})


@dataclass(frozen=True)
class OpenApiOperation:
    method: str
    path: str
    operation_id: str
    response_schema: Mapping[str, object]


class OpenApiRecipeProfile:
    """Allowlisted OpenAPI operations for one operator-selected endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        operations: Mapping[tuple[str, str], OpenApiOperation],
        max_response_bytes: int = 256 * 1024,
        reference_registry: Registry[Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._operations = dict(operations)
        self.max_response_bytes = max_response_bytes
        self._reference_registry = reference_registry

    @classmethod
    def from_document(
        cls,
        document: Mapping[str, object],
        *,
        endpoint: str,
        allowed_hosts: frozenset[str] = frozenset(),
        max_document_bytes: int = _MAX_DOCUMENT_BYTES,
        max_response_bytes: int = 256 * 1024,
    ) -> OpenApiRecipeProfile:
        if max_document_bytes <= 0 or max_response_bytes <= 0:
            raise ValueError("REST size limits must be positive")
        try:
            encoded = json.dumps(document, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("OpenAPI document is invalid") from error
        if len(encoded) > max_document_bytes:
            raise ValueError("OpenAPI document is too large")
        cls._validate_endpoint(endpoint, allowed_hosts)
        openapi = document.get("openapi")
        if not isinstance(openapi, str) or not openapi.startswith(("3.0", "3.1")):
            raise ValueError("OpenAPI 3.0 or 3.1 document is required")
        paths = document.get("paths")
        if not isinstance(paths, Mapping) or not paths:
            raise ValueError("OpenAPI paths are required")
        cls._validate_servers(document.get("servers"), allowed_hosts)

        reference_uri = f"urn:domoai:openapi-profile:{uuid4().hex}"
        closed_document = _rewrite_local_references(document, reference_uri)
        if not isinstance(closed_document, Mapping):  # pragma: no cover - guarded by input
            raise ValueError("OpenAPI document must be an object")
        reference_registry = Registry().with_resource(
            reference_uri,
            Resource.from_contents(closed_document, default_specification=DRAFT202012),
        )
        _validate_local_reference_targets(closed_document, reference_registry, reference_uri)

        operations: dict[tuple[str, str], OpenApiOperation] = {}
        for raw_path, raw_item in paths.items():
            if not isinstance(raw_path, str):
                raise ValueError("OpenAPI path must be a string")
            cls._validate_path(raw_path)
            if not isinstance(raw_item, Mapping):
                raise ValueError("OpenAPI path item must be an object")
            for raw_method, raw_operation in raw_item.items():
                if raw_method in _PATH_ITEM_METADATA or str(raw_method).startswith("x-"):
                    continue
                method = str(raw_method).upper()
                if method not in _ALLOWED_METHODS:
                    raise ValueError("HTTP method is not allowed")
                if not isinstance(raw_operation, Mapping):
                    raise ValueError("OpenAPI operation must be an object")
                response_schema = _rewrite_local_references(
                    cls._response_schema(raw_operation.get("responses")), reference_uri
                )
                if not isinstance(response_schema, Mapping):  # pragma: no cover
                    raise ValueError("OpenAPI response schema is invalid")
                operation_id = raw_operation.get("operationId", f"{method} {raw_path}")
                if not isinstance(operation_id, str) or not operation_id.strip():
                    raise ValueError("OpenAPI operationId is invalid")
                operations[(method, raw_path)] = OpenApiOperation(
                    method=method,
                    path=raw_path,
                    operation_id=operation_id,
                    response_schema=response_schema,
                )
        if not operations:
            raise ValueError("OpenAPI document declares no usable operations")
        return cls(
            base_url=endpoint,
            operations=operations,
            max_response_bytes=max_response_bytes,
            reference_registry=reference_registry,
        )

    @classmethod
    def from_text(
        cls,
        payload: bytes,
        *,
        endpoint: str,
        content_type: str = "application/json",
        allowed_hosts: frozenset[str] = frozenset(),
        max_document_bytes: int = _MAX_DOCUMENT_BYTES,
        max_response_bytes: int = 256 * 1024,
    ) -> OpenApiRecipeProfile:
        if len(payload) > max_document_bytes:
            raise ValueError("OpenAPI document is too large")
        try:
            document = (
                json.loads(payload)
                if "json" in content_type.casefold()
                else yaml.safe_load(payload)
            )
        except (ValueError, TypeError, yaml.YAMLError) as error:
            raise ValueError("OpenAPI document is invalid") from error
        if not isinstance(document, Mapping):
            raise ValueError("OpenAPI document must be an object")
        return cls.from_document(
            document,
            endpoint=endpoint,
            allowed_hosts=allowed_hosts,
            max_document_bytes=max_document_bytes,
            max_response_bytes=max_response_bytes,
        )

    def operation(self, method: str, path: str) -> OpenApiOperation:
        try:
            return self._operations[(method.upper(), path)]
        except KeyError as error:
            raise ValueError("REST operation is not declared by the OpenAPI profile") from error

    def validate_response(self, method: str, path: str, payload: object) -> None:
        operation = self.operation(method, path)
        try:
            Draft202012Validator(
                operation.response_schema, registry=self._reference_registry
            ).validate(payload)
        except Exception as error:
            raise ValueError("REST response does not satisfy its declared schema") from error

    @staticmethod
    def _validate_endpoint(endpoint: str, allowed_hosts: frozenset[str]) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("REST endpoint must be an HTTP URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("REST endpoint must not contain credentials")
        host = parsed.hostname.casefold()
        if parsed.scheme != "https" and host not in _LOCAL_HOSTS:
            raise ValueError("HTTPS is required for remote REST endpoints")
        if allowed_hosts and host not in {item.casefold() for item in allowed_hosts}:
            raise ValueError("REST endpoint host is not in the allowlist")

    @classmethod
    def _validate_servers(
        cls, servers: object, allowed_hosts: frozenset[str]
    ) -> None:
        if servers is None:
            return
        if not isinstance(servers, list):
            raise ValueError("OpenAPI servers must be a list")
        for server in servers:
            if not isinstance(server, Mapping) or not isinstance(server.get("url"), str):
                raise ValueError("OpenAPI server URL is invalid")
            cls._validate_endpoint(server["url"], allowed_hosts)

    @staticmethod
    def _validate_path(path: str) -> None:
        if not path.startswith("/") or "//" in path:
            raise ValueError("OpenAPI path is invalid")
        decoded = unquote(path)
        if any(segment == ".." for segment in decoded.split("/")):
            raise ValueError("OpenAPI path traversal is not allowed")
        for variable in _template_variables(path):
            if variable != "device_id":
                raise ValueError("OpenAPI path variable is not allowed")

    @staticmethod
    def _response_schema(responses: object) -> Mapping[str, object]:
        if not isinstance(responses, Mapping):
            raise ValueError("OpenAPI response schema is required")
        for status, response in responses.items():
            if not (isinstance(status, str) and status.isdigit() and 200 <= int(status) < 300):
                continue
            if not isinstance(response, Mapping):
                continue
            content = response.get("content")
            if not isinstance(content, Mapping):
                continue
            json_content = content.get("application/json")
            if not isinstance(json_content, Mapping):
                continue
            schema = json_content.get("schema")
            if (
                isinstance(schema, Mapping)
                and schema
                and (
                    isinstance(schema.get("type"), str)
                    or any(key in schema for key in _REFERENCE_KEYS)
                )
            ):
                return schema
        raise ValueError("OpenAPI response schema is required")


def _template_variables(value: str) -> set[str]:
    variables: set[str] = set()
    start = 0
    while True:
        opening = value.find("{", start)
        if opening < 0:
            break
        closing = value.find("}", opening + 1)
        if closing < 0:
            raise ValueError("OpenAPI path template is invalid")
        name = value[opening + 1 : closing]
        if not name or "{" in name or "}" in name:
            raise ValueError("OpenAPI path template is invalid")
        variables.add(name)
        start = closing + 1
    if "}" in value[start:]:
        raise ValueError("OpenAPI path template is invalid")
    return variables


def _rewrite_local_references(value: object, base_uri: str) -> object:
    if isinstance(value, Mapping):
        rewritten: dict[object, object] = {}
        for key, nested in value.items():
            if key in _REFERENCE_KEYS:
                if not isinstance(nested, str) or not nested.startswith("#"):
                    raise ValueError("OpenAPI remote schema references are not allowed")
                rewritten[key] = base_uri + nested
            else:
                rewritten[key] = _rewrite_local_references(nested, base_uri)
        return rewritten
    if isinstance(value, list):
        return [_rewrite_local_references(item, base_uri) for item in value]
    return value


def _validate_local_reference_targets(
    document: Mapping[object, object], registry: Registry[Any], base_uri: str
) -> None:
    resolver = registry.resolver(base_uri)

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if key in _REFERENCE_KEYS:
                    if not isinstance(nested, str):  # pragma: no cover - rewrite guards this
                        raise ValueError("OpenAPI schema reference is invalid")
                    try:
                        resolver.lookup(nested)
                    except Exception as error:
                        raise ValueError("OpenAPI local schema reference is unresolved") from error
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(document)


__all__ = ["OpenApiOperation", "OpenApiRecipeProfile"]
