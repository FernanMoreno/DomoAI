"""Local JSON management API for the connection control plane."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast
from urllib.parse import urlparse
from uuid import UUID

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from domoai.adapters.home_assistant.oauth import HomeAssistantOAuthConfig
from domoai.adapters.knx.ets import EtsImporter, EtsImportError
from domoai.adapters.mqtt.discovery import MqttDiscoveryPreview
from domoai.application.connection_manager import ConnectionManagerError
from domoai.application.connection_providers import ConnectionSetup
from domoai.application.home_assistant_oauth import (
    HomeAssistantOAuthServiceError,
)
from domoai.application.home_assistant_oauth_discovery import (
    HomeAssistantOAuthMetadataService,
)
from domoai.application.knx_secure import KnxSecureError
from domoai.application.repair_engine import RepairEngineError
from domoai.domain.connections import (
    ConnectionCandidate,
    ConnectionDraft,
    ConnectionProfile,
    ProviderCatalogEntry,
)
from domoai.domain.p1_completion import CommissioningMethod
from domoai.domain.p2_connections import EtsImportPreview, OAuthCredentialRecord
from domoai.domain.repairs import RepairPlan
from domoai.management.auth import ManagementAuth
from domoai.management.rate_limit import RateLimiter
from domoai.management.ui import first_install_page

_CONTROL_OPERATIONS = frozenset({"activate", "disable", "reauth", "repair"})


def create_management_app(
    manager: Any,
    *,
    ets_importer: EtsImporter | None = None,
    knx_secure_credentials: Any | None = None,
    home_assistant_oauth: Any | None = None,
    home_assistant_oauth_config: HomeAssistantOAuthConfig | None = None,
    home_assistant_oauth_discovery: HomeAssistantOAuthMetadataService | None = None,
    repair_engine: Any | None = None,
    mqtt_discovery: Any | None = None,
    matter_commissioning: Any | None = None,
    host: str = "127.0.0.1",
    auth_token: str | None = None,
    tls_enabled: bool = False,
    rate_limit: int = 60,
    auth_verifier: Callable[[str], Awaitable[bool]] | None = None,
) -> Starlette:
    """Create a local-first management app around one ConnectionManager."""

    ets_importer = ets_importer or EtsImporter()
    auth = ManagementAuth(
        host,
        token=auth_token,
        tls_enabled=tls_enabled,
        external_auth=auth_verifier is not None,
    )
    limiter = RateLimiter(rate_limit)

    async def protected(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        authorization = request.headers.get("authorization")
        if auth_verifier is not None:
            token = _bearer_token(authorization)
            authorized = token is not None and await auth_verifier(token)
        else:
            authorized = auth.authorized(authorization)
        if not authorized:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not _same_origin(request):
            return JSONResponse({"error": "origin_forbidden"}, status_code=403)
        client_host = request.client.host if request.client is not None else "unknown"
        if not limiter.allow(client_host):
            return JSONResponse({"error": "rate_limited"}, status_code=429)
        return await call_next(request)

    async def ui(_request: Request) -> Response:
        return first_install_page()

    async def list_connections(_request: Request) -> Response:
        profiles = await manager.list()
        return JSONResponse({"connections": [_profile_payload(profile) for profile in profiles]})

    async def list_provider_catalog(_request: Request) -> Response:
        providers = getattr(manager, "providers", None)
        catalog = getattr(providers, "catalog", ()) if providers is not None else ()
        if callable(catalog):
            catalog = catalog()
        if not catalog and providers is not None:
            catalog = tuple(
                ProviderCatalogEntry.from_manifest(provider.manifest)
                for provider in getattr(providers, "providers", ())
            )
        diagnostics = getattr(providers, "diagnostics", ()) if providers is not None else ()
        return JSONResponse(
            {
                "providers": [_model_payload(item) for item in catalog],
                "diagnostics": [_model_payload(item) for item in diagnostics],
            }
        )

    async def discover(request: Request) -> Response:
        try:
            body = await _json_body(request)
            candidates = await manager.discover(_required_string(body, "connector_id"))
            return JSONResponse(
                {"candidates": [_model_payload(candidate) for candidate in candidates]}
            )
        except Exception as error:  # noqa: BLE001
            return _error_response(error)

    async def setup_session(request: Request) -> Response:
        try:
            body = await _json_body(request)
            action = str(body.get("action", "start")).casefold()
            if action == "start":
                session_id, candidates = await manager.start_setup(
                    _required_string(body, "connector_id")
                )
                return JSONResponse(
                    {
                        "session_id": str(session_id),
                        "candidates": [_model_payload(candidate) for candidate in candidates],
                    },
                    status_code=201,
                )
            session_id = _body_uuid(body, "session_id")
            if action == "select":
                candidate = await manager.select_setup(
                    session_id, _required_string(body, "candidate_id")
                )
                return JSONResponse({"candidate": _model_payload(candidate)})
            if action == "setup":
                credential = body.get("credential")
                if credential is not None and not isinstance(credential, str):
                    raise ValueError("credential must be a string")
                credential_ref = body.get("credential_ref")
                if credential_ref is not None and not isinstance(credential_ref, str):
                    raise ValueError("credential_ref must be a string")
                endpoint = body.get("endpoint", {})
                options = body.get("options", {})
                if not isinstance(endpoint, Mapping) or not isinstance(options, Mapping):
                    raise ValueError("endpoint and options must be objects")
                draft = await manager.setup_draft(
                    session_id,
                    endpoint=endpoint,
                    options=options,
                    mapping_ref=body.get("mapping_ref"),
                    credential_ref=credential_ref,
                    credential_value=credential,
                )
                return JSONResponse({"draft": _model_payload(draft)})
            if action == "validate":
                result = await manager.validate_setup(session_id)
                return JSONResponse(_model_payload(result))
            if action == "create":
                activate = body.get("activate", True)
                if not isinstance(activate, bool):
                    raise ValueError("activate must be a boolean")
                profile = await manager.create_setup(session_id, activate=activate)
                return JSONResponse(
                    {"connection": _profile_payload(profile)}, status_code=201
                )
            raise ValueError("setup action is not supported")
        except Exception as error:  # noqa: BLE001
            return _error_response(error)

    async def create_connection(request: Request) -> Response:
        try:
            body = await _json_body(request)
            connector_id = _required_string(body, "connector_id")
            endpoint = body.get("endpoint", {})
            options = body.get("options", {})
            if not isinstance(endpoint, Mapping) or not isinstance(options, Mapping):
                raise ValueError("endpoint and options must be objects")
            requested_id = body.get("candidate_id") or body.get("unique_id")
            if requested_id is not None and not isinstance(requested_id, str):
                raise ValueError("candidate_id must be a string")
            candidate_id = requested_id or _manual_candidate_id(connector_id, endpoint, options)
            credential = body.get("credential")
            if credential is not None and not isinstance(credential, str):
                raise ValueError("credential must be a string")
            mapping_ref = body.get("mapping_ref")
            if mapping_ref is not None and not isinstance(mapping_ref, str):
                raise ValueError("mapping_ref must be a string")
            candidate = ConnectionCandidate(
                connector_id=connector_id,
                candidate_id=candidate_id,
                display_name=str(body.get("display_name") or connector_id),
                endpoint=dict(endpoint),
                metadata={"source": "manual"},
            )
            draft = ConnectionDraft(
                connector_id=connector_id,
                unique_id=candidate_id,
                endpoint=dict(endpoint),
                options=dict(options),
                mapping_ref=mapping_ref,
            )
            setup = ConnectionSetup(candidate=candidate, draft=draft)
            kwargs: dict[str, object] = {"credential_value": credential}
            if "activate" in body:
                activate = body["activate"]
                if not isinstance(activate, bool):
                    raise ValueError("activate must be a boolean")
                kwargs["activate"] = activate
            profile = await manager.create(setup, **kwargs)
            return JSONResponse({"connection": _profile_payload(profile)}, status_code=201)
        except Exception as error:  # noqa: BLE001 - edge converts to safe HTTP errors
            return _error_response(error)

    async def validate_connection(request: Request) -> Response:
        try:
            profile = await _profile(manager, request)
            result = await manager.validate(_setup_for(profile))
            return JSONResponse(_model_payload(result))
        except Exception as error:  # noqa: BLE001
            return _error_response(error)

    async def operation(request: Request) -> Response:
        try:
            connection_id = _path_uuid(request)
            method_name = request.path_params["operation"]
            if method_name not in _CONTROL_OPERATIONS:
                raise ValueError("connection operation is not supported")
            if method_name == "reauth":
                body = await _json_body(request)
                result = await manager.reauth(connection_id, _required_string(body, "credential"))
            else:
                result = await getattr(manager, method_name)(connection_id)
            return JSONResponse(_model_payload(result))
        except Exception as error:  # noqa: BLE001
            return _error_response(error)

    async def reconfigure(request: Request) -> Response:
        try:
            body = await _json_body(request)
            current = await manager.get(_path_uuid(request))
            endpoint = body.get("endpoint", {})
            options = body.get("options", {})
            mapping_ref = body.get("mapping_ref", current.mapping_ref)
            if not isinstance(endpoint, Mapping) or not isinstance(options, Mapping):
                raise ValueError("endpoint and options must be objects")
            if mapping_ref is not None and not isinstance(mapping_ref, str):
                raise ValueError("mapping_ref must be a string")
            result = await manager.reconfigure(
                _path_uuid(request),
                endpoint=endpoint,
                options=options,
                mapping_ref=mapping_ref,
            )
            return JSONResponse({"connection": _profile_payload(result)})
        except Exception as error:  # noqa: BLE001
            return _error_response(error)

    async def diagnostics(request: Request) -> Response:
        try:
            result = await manager.diagnose(_path_uuid(request))
            return JSONResponse({"diagnostics": [_model_payload(item) for item in result]})
        except Exception as error:  # noqa: BLE001
            return _error_response(error)

    async def import_legacy(request: Request) -> Response:
        try:
            if await request.body():
                body = await _json_body(request)
                if body:
                    raise ValueError("legacy import does not accept request values")
            importer = getattr(manager, "import_legacy", None)
            if not callable(importer):
                raise ConnectionManagerError(
                    "legacy_import_unavailable", "legacy import is unavailable"
                )
            report = await importer()
            return JSONResponse(_model_payload(report))
        except Exception as error:  # noqa: BLE001
            return _error_response(error)

    async def mqtt_discovery_preview(_request: Request) -> Response:
        try:
            if mqtt_discovery is None:
                raise ConnectionManagerError(
                    "mqtt_discovery_unavailable", "MQTT Discovery is unavailable"
                )
            preview = await mqtt_discovery.preview()
            return JSONResponse(_model_payload(preview))
        except Exception as error:  # noqa: BLE001
            return _error_response(error)

    async def ets_preview(request: Request) -> Response:
        try:
            body = await _json_body(request)
            source_path = _required_string(body, "source_path")
            preview = ets_importer.preview(source_path)
            return JSONResponse(_model_payload(preview))
        except Exception as error:  # noqa: BLE001 - importer returns safe errors
            return _error_response(error)

    async def ets_persist(request: Request) -> Response:
        try:
            body = await _json_body(request)
            preview_payload = body.get("preview")
            if not isinstance(preview_payload, Mapping):
                raise ValueError("preview is required")
            source_path = _required_string(body, "source_path")
            target_path = _required_string(body, "target_path")
            confirmed = body.get("confirmed")
            if not isinstance(confirmed, bool):
                raise ValueError("confirmed must be a boolean")
            preview = EtsImportPreview.model_validate(preview_payload)
            target = ets_importer.persist(
                preview,
                source_path=source_path,
                target_path=target_path,
                confirmed=confirmed,
            )
            return JSONResponse({"mapping_ref": str(target)}, status_code=201)
        except Exception as error:  # noqa: BLE001 - importer returns safe errors
            return _error_response(error)

    async def knx_secure_rotate(request: Request) -> Response:
        try:
            if knx_secure_credentials is None:
                raise ConnectionManagerError(
                    "knx_secure_unavailable", "KNX Secure credential service is unavailable"
                )
            body = await _json_body(request)
            old_reference = _required_string(body, "old_reference")
            values: dict[str, str | None] = {}
            for key in (
                "keyring_password",
                "user_password",
                "device_authentication_password",
                "backbone_key",
            ):
                value = body.get(key)
                if value is not None and (not isinstance(value, str) or not value):
                    raise ValueError(f"{key} must be a non-empty string")
                values[key] = value
            if not any(value is not None for value in values.values()):
                raise ValueError("new KNX Secure credentials are required")
            reference = knx_secure_credentials.rotate(old_reference, **values)
            return JSONResponse({"credential_ref": reference}, status_code=201)
        except Exception as error:  # noqa: BLE001
            return _error_response(error)

    async def knx_secure_diagnostics(request: Request) -> Response:
        try:
            connection_id = request.query_params.get("connection_id")
            if not connection_id:
                raise ValueError("connection_id is required")
            result = await manager.diagnose(UUID(connection_id))
            return JSONResponse({"diagnostics": [_model_payload(item) for item in result]})
        except Exception as error:  # noqa: BLE001
            return _error_response(error)

    async def oauth_start(request: Request) -> Response:
        try:
            if home_assistant_oauth is None:
                raise ConnectionManagerError(
                    "home_assistant_oauth_unavailable", "Home Assistant OAuth is unavailable"
                )
            body = await _json_body(request)
            config = await _oauth_config_for_start(
                body,
                home_assistant_oauth_config,
                home_assistant_oauth_discovery,
            )
            result = home_assistant_oauth.begin(config)
            return JSONResponse(_model_payload(result))
        except Exception as error:  # noqa: BLE001
            return _error_response(error)

    async def oauth_callback(request: Request) -> Response:
        try:
            if home_assistant_oauth is None:
                raise ConnectionManagerError(
                    "home_assistant_oauth_unavailable", "Home Assistant OAuth is unavailable"
                )
            body = await _json_body(request)
            result = await home_assistant_oauth.complete(
                _oauth_config_optional(body, home_assistant_oauth_config),
                state=_required_string(body, "state"),
                code=_required_string(body, "code"),
            )
            return JSONResponse(_model_payload(result), status_code=201)
        except Exception as error:  # noqa: BLE001
            return _error_response(error)

    async def oauth_discover(request: Request) -> Response:
        try:
            if home_assistant_oauth_discovery is None:
                raise ConnectionManagerError(
                    "home_assistant_oauth_discovery_unavailable",
                    "Home Assistant OAuth metadata discovery is unavailable",
                )
            body = await _json_body(request)
            base_url = _required_string(body, "base_url")
            metadata_url = body.get("metadata_url")
            if metadata_url is not None and not isinstance(metadata_url, str):
                raise ValueError("metadata_url must be a string")
            result = await home_assistant_oauth_discovery.discover(
                base_url, metadata_url=metadata_url
            )
            return JSONResponse(_model_payload(result))
        except Exception as error:  # noqa: BLE001
            return _error_response(error)

    async def oauth_refresh(request: Request) -> Response:
        try:
            if home_assistant_oauth is None:
                raise ConnectionManagerError(
                    "home_assistant_oauth_unavailable", "Home Assistant OAuth is unavailable"
                )
            body = await _json_body(request)
            config = _oauth_config(body, home_assistant_oauth_config)
            result = await home_assistant_oauth.refresh(
                config, _oauth_record(body)
            )
            return JSONResponse(_model_payload(result))
        except Exception as error:  # noqa: BLE001
            return _error_response(error)

    async def oauth_revoke(request: Request) -> Response:
        try:
            if home_assistant_oauth is None:
                raise ConnectionManagerError(
                    "home_assistant_oauth_unavailable", "Home Assistant OAuth is unavailable"
                )
            body = await _json_body(request)
            config = _oauth_config(body, home_assistant_oauth_config)
            await home_assistant_oauth.revoke(config, _oauth_record(body))
            return Response(status_code=204)
        except Exception as error:  # noqa: BLE001
            return _error_response(error)

    async def oauth_migrate(request: Request) -> Response:
        try:
            if home_assistant_oauth is None:
                raise ConnectionManagerError(
                    "home_assistant_oauth_unavailable", "Home Assistant OAuth is unavailable"
                )
            body = await _json_body(request)
            config = _oauth_config(body, home_assistant_oauth_config)
            result = home_assistant_oauth.migrate(config, _required_string(body, "token"))
            return JSONResponse(_model_payload(result), status_code=201)
        except Exception as error:  # noqa: BLE001
            return _error_response(error)

    async def oauth_reauth(request: Request) -> Response:
        try:
            if home_assistant_oauth is None:
                raise ConnectionManagerError(
                    "home_assistant_oauth_unavailable", "Home Assistant OAuth is unavailable"
                )
            reauth_ref = getattr(manager, "reauth_ref", None)
            if not callable(reauth_ref):
                raise ConnectionManagerError(
                    "oauth_reauth_unavailable", "OAuth reauthentication is unavailable"
                )
            body = await _json_body(request)
            record = _oauth_record(body)
            home_assistant_oauth.validate_record(record)
            result = await reauth_ref(
                UUID(_required_string(body, "connection_id")), record.credential_ref
            )
            return JSONResponse({"connection": _profile_payload(result)})
        except Exception as error:  # noqa: BLE001
            return _error_response(error)

    async def repair_plan(request: Request) -> Response:
        try:
            if repair_engine is None:
                raise ConnectionManagerError(
                    "repair_engine_unavailable", "Repair Engine is unavailable"
                )
            result = await repair_engine.plan(_path_uuid(request))
            return JSONResponse(_model_payload(result))
        except Exception as error:  # noqa: BLE001
            return _error_response(error)

    async def repair_execute(request: Request) -> Response:
        try:
            if repair_engine is None:
                raise ConnectionManagerError(
                    "repair_engine_unavailable", "Repair Engine is unavailable"
                )
            body = await _json_body(request)
            plan_payload = body.get("plan")
            if not isinstance(plan_payload, Mapping):
                raise ValueError("plan is required")
            confirmed = body.get("confirmed", False)
            if not isinstance(confirmed, bool):
                raise ValueError("confirmed must be a boolean")
            parameters = body.get("parameters")
            if parameters is not None and not isinstance(parameters, Mapping):
                raise ValueError("parameters must be an object")
            credential = body.get("credential")
            if credential is not None and not isinstance(credential, str):
                raise ValueError("credential must be a string")
            plan = RepairPlan.model_validate(plan_payload)
            result = await repair_engine.execute(
                plan,
                request.path_params["action_id"],
                confirmed=confirmed,
                parameters=parameters,
                credential=credential,
            )
            return JSONResponse(_model_payload(result))
        except Exception as error:  # noqa: BLE001
            return _error_response(error)

    async def mqtt_discovery_persist(request: Request) -> Response:
        try:
            if mqtt_discovery is None:
                raise ConnectionManagerError(
                    "mqtt_discovery_unavailable", "MQTT Discovery is unavailable"
                )
            body = await _json_body(request)
            preview_payload = body.get("preview")
            if not isinstance(preview_payload, Mapping):
                raise ValueError("preview is required")
            confirmed = body.get("confirmed")
            if not isinstance(confirmed, bool):
                raise ValueError("confirmed must be a boolean")
            preview = MqttDiscoveryPreview.model_validate(preview_payload)
            stored = await mqtt_discovery.persist(preview, confirmed=confirmed)
            mapping_ref = getattr(stored, "document_ref", stored)
            if not isinstance(mapping_ref, str):
                raise ValueError("MQTT Discovery persistence returned an invalid reference")
            return JSONResponse({"mapping_ref": mapping_ref}, status_code=201)
        except Exception as error:  # noqa: BLE001
            return _error_response(error)

    async def matter_commissioning_start(request: Request) -> Response:
        try:
            if matter_commissioning is None:
                raise ConnectionManagerError(
                    "matter_commissioning_unavailable",
                    "Matter commissioning is unavailable",
                )
            body = await _json_body(request)
            method_value = _required_string(body, "method").casefold()
            try:
                method = CommissioningMethod(method_value)
            except ValueError:
                raise ValueError("method is invalid") from None
            connection_id = _required_string(body, "connection_id")
            code = _required_string(body, "code")
            can_commission = body.get("can_commission")
            if not isinstance(can_commission, bool):
                raise ValueError("can_commission must be a boolean")
            prerequisites = body.get("prerequisites")
            if not isinstance(prerequisites, Mapping) or not all(
                isinstance(key, str) and isinstance(value, bool)
                for key, value in prerequisites.items()
            ):
                raise ValueError("prerequisites must be a boolean object")
            confirmed = body.get("confirmed", False)
            if not isinstance(confirmed, bool):
                raise ValueError("confirmed must be a boolean")
            session = await matter_commissioning.start(
                connection_id,
                method=method,
                code=code,
                can_commission=can_commission,
                prerequisites=dict(prerequisites),
                confirmed=confirmed,
            )
            return JSONResponse(_model_payload(session))
        except Exception as error:  # noqa: BLE001
            return _error_response(error)

    async def matter_commissioning_action(request: Request) -> Response:
        try:
            if matter_commissioning is None:
                raise ConnectionManagerError(
                    "matter_commissioning_unavailable",
                    "Matter commissioning is unavailable",
                )
            session_id = str(request.path_params["session_id"])
            action = request.path_params["action"]
            if action == "confirm":
                session = await matter_commissioning.confirm(session_id)
            elif action == "cleanup":
                session = await matter_commissioning.cleanup(session_id)
            else:
                raise ValueError("Matter commissioning action is not supported")
            return JSONResponse(_model_payload(session))
        except Exception as error:  # noqa: BLE001
            return _error_response(error)

    async def delete_connection(request: Request) -> Response:
        try:
            deleted = await manager.delete(_path_uuid(request))
            return Response(status_code=204 if deleted else 404)
        except Exception as error:  # noqa: BLE001
            return _error_response(error)

    routes = [
        Route("/", ui, methods=["GET"]),
        Route("/api/v1/connections", list_connections, methods=["GET"]),
        Route("/api/v1/connection-providers", list_provider_catalog, methods=["GET"]),
        Route("/api/v1/connections", create_connection, methods=["POST"]),
        Route("/api/v1/connections/setup", setup_session, methods=["POST"]),
        Route("/api/v1/connections/discovery", discover, methods=["POST"]),
        Route("/api/v1/legacy/import", import_legacy, methods=["POST"]),
        Route(
            "/api/v1/mqtt/discovery/preview",
            mqtt_discovery_preview,
            methods=["POST"],
        ),
        Route(
            "/api/v1/mqtt/discovery/persist",
            mqtt_discovery_persist,
            methods=["POST"],
        ),
        Route("/api/v1/knx/ets/preview", ets_preview, methods=["POST"]),
        Route("/api/v1/knx/ets/persist", ets_persist, methods=["POST"]),
        Route("/api/v1/knx/secure/rotate", knx_secure_rotate, methods=["POST"]),
        Route("/api/v1/knx/secure/diagnostics", knx_secure_diagnostics, methods=["GET"]),
        Route("/api/v1/home-assistant/oauth/start", oauth_start, methods=["POST"]),
        Route("/api/v1/home-assistant/oauth/discover", oauth_discover, methods=["POST"]),
        Route("/api/v1/home-assistant/oauth/callback", oauth_callback, methods=["POST"]),
        Route("/api/v1/home-assistant/oauth/refresh", oauth_refresh, methods=["POST"]),
        Route("/api/v1/home-assistant/oauth/revoke", oauth_revoke, methods=["POST"]),
        Route("/api/v1/home-assistant/oauth/migrate", oauth_migrate, methods=["POST"]),
        Route("/api/v1/home-assistant/oauth/reauth", oauth_reauth, methods=["POST"]),
        Route(
            "/api/v1/matter/commissioning/start",
            matter_commissioning_start,
            methods=["POST"],
        ),
        Route(
            "/api/v1/matter/commissioning/{session_id:str}/{action:str}",
            matter_commissioning_action,
            methods=["POST"],
        ),
        Route(
            "/api/v1/connections/{connection_id:uuid}/validate",
            validate_connection,
            methods=["POST"],
        ),
        Route(
            "/api/v1/connections/{connection_id:uuid}/repairs",
            repair_plan,
            methods=["GET"],
        ),
        Route(
            "/api/v1/connections/{connection_id:uuid}/repairs/{action_id:str}",
            repair_execute,
            methods=["POST"],
        ),
        Route(
            "/api/v1/connections/{connection_id:uuid}",
            reconfigure,
            methods=["PATCH"],
        ),
        Route(
            "/api/v1/connections/{connection_id:uuid}/{operation:str}",
            operation,
            methods=["POST"],
        ),
        Route(
            "/api/v1/connections/{connection_id:uuid}/diagnostics",
            diagnostics,
            methods=["GET"],
        ),
        Route(
            "/api/v1/connections/{connection_id:uuid}",
            delete_connection,
            methods=["DELETE"],
        ),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(BaseHTTPMiddleware, dispatch=protected)
    return app


async def _json_body(request: Request) -> dict[str, Any]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise ValueError("request body must be an object")
    return payload


def _required_string(body: Mapping[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value


def _body_uuid(body: Mapping[str, Any], key: str) -> UUID:
    value = _required_string(body, key)
    try:
        return UUID(value)
    except ValueError:
        raise ValueError(f"{key} is invalid") from None


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True
    parsed = urlparse(origin)
    request_host = request.headers.get("host")
    return parsed.scheme in {"http", "https"} and parsed.netloc == request_host


def _bearer_token(header: str | None) -> str | None:
    if header is None:
        return None
    scheme, separator, value = header.partition(" ")
    if scheme.casefold() != "bearer" or not separator or not value.strip():
        return None
    return value.strip()


async def _profile(manager: Any, request: Request) -> ConnectionProfile:
    return cast(ConnectionProfile, await manager.get(_path_uuid(request)))


def _path_uuid(request: Request) -> UUID:
    value = request.path_params["connection_id"]
    return value if isinstance(value, UUID) else UUID(str(value))


def _setup_for(profile: ConnectionProfile) -> ConnectionSetup:
    candidate = ConnectionCandidate(
        connector_id=profile.connector_id,
        candidate_id=profile.unique_id,
        display_name=profile.unique_id,
        endpoint=profile.endpoint,
    )
    return ConnectionSetup(
        candidate=candidate,
        draft=ConnectionDraft(
            connector_id=profile.connector_id,
            unique_id=profile.unique_id,
            endpoint=profile.endpoint,
            options=profile.options,
            credential_ref=profile.credential_ref,
            mapping_ref=profile.mapping_ref,
        ),
    )


def _profile_payload(profile: ConnectionProfile) -> dict[str, object]:
    return {
        "id": str(profile.id),
        "connector_id": profile.connector_id,
        "unique_id": profile.unique_id,
        "endpoint": profile.endpoint,
        "options": profile.options,
        "credential_configured": profile.credential_ref is not None,
        "mapping_ref": profile.mapping_ref,
        "enabled": profile.enabled,
        "schema_version": profile.schema_version,
        "created_at": profile.created_at.isoformat(),
        "updated_at": profile.updated_at.isoformat(),
    }


def _manual_candidate_id(
    connector_id: str, endpoint: Mapping[str, object], options: Mapping[str, object]
) -> str:
    canonical = json.dumps(
        {"connector_id": connector_id, "endpoint": dict(endpoint), "options": dict(options)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{connector_id}:manual:{hashlib.sha256(canonical.encode()).hexdigest()[:16]}"


def _oauth_config(
    body: Mapping[str, Any], default: HomeAssistantOAuthConfig | None
) -> HomeAssistantOAuthConfig:
    if default is not None:
        return default
    config = body.get("config")
    if not isinstance(config, Mapping):
        raise ValueError("OAuth configuration is required")
    return HomeAssistantOAuthConfig.model_validate(config)


def _oauth_config_optional(
    body: Mapping[str, Any], default: HomeAssistantOAuthConfig | None
) -> HomeAssistantOAuthConfig | None:
    if default is not None:
        return default
    config = body.get("config")
    if config is None:
        return None
    if not isinstance(config, Mapping):
        raise ValueError("OAuth configuration is invalid")
    return HomeAssistantOAuthConfig.model_validate(config)


async def _oauth_config_for_start(
    body: Mapping[str, Any],
    default: HomeAssistantOAuthConfig | None,
    discovery: HomeAssistantOAuthMetadataService | None,
) -> HomeAssistantOAuthConfig:
    if default is not None:
        return default
    config = body.get("config")
    if config is not None:
        if not isinstance(config, Mapping):
            raise ValueError("OAuth configuration is invalid")
        return HomeAssistantOAuthConfig.model_validate(config)
    base_url = body.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("OAuth configuration is required")
    if discovery is None:
        raise ValueError("OAuth metadata discovery is unavailable")
    metadata_url = body.get("metadata_url")
    if metadata_url is not None and not isinstance(metadata_url, str):
        raise ValueError("metadata_url must be a string")
    result = await discovery.discover(base_url, metadata_url=metadata_url)
    if result.metadata is None:
        raise ValueError("OAuth metadata is unavailable; enter the provider fields manually")
    client_id = body.get("client_id")
    redirect_uri = body.get("redirect_uri")
    if not isinstance(client_id, str) or not client_id.strip():
        raise ValueError("OAuth client_id is required")
    if not isinstance(redirect_uri, str) or not redirect_uri.strip():
        raise ValueError("OAuth redirect_uri is required")
    scopes = body.get("scopes", result.metadata.scopes)
    if isinstance(scopes, str):
        scopes = tuple(item for item in scopes.split() if item)
    if not isinstance(scopes, (list, tuple)) or not all(
        isinstance(scope, str) and scope.strip() for scope in scopes
    ):
        raise ValueError("OAuth scopes are invalid")
    return HomeAssistantOAuthConfig(
        authorization_url=result.metadata.authorization_url,
        token_url=result.metadata.token_url,
        revoke_url=result.metadata.revoke_url,
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=tuple(scopes),
    )


def _oauth_record(body: Mapping[str, Any]) -> OAuthCredentialRecord:
    record = body.get("record")
    if not isinstance(record, Mapping):
        raise ValueError("OAuth credential record is required")
    return OAuthCredentialRecord.model_validate(record)


def _model_payload(value: object) -> object:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return cast(Callable[..., object], model_dump)(mode="json")
    if isinstance(value, (list, tuple)):
        return [_model_payload(item) for item in value]
    return value


def _error_response(error: Exception) -> JSONResponse:
    if isinstance(error, EtsImportError):
        message = str(error)
        status = 409 if "confirmation" in message or "digest" in message else 400
        return JSONResponse({"error": "ets_import_failed", "message": message}, status_code=status)
    if isinstance(error, ConnectionManagerError):
        status = 404 if error.code == "not_found" else 409
        return JSONResponse({"error": error.code, "message": str(error)}, status_code=status)
    if isinstance(error, (HomeAssistantOAuthServiceError, RepairEngineError, KnxSecureError)):
        return JSONResponse(
            {"error": "operation_rejected", "message": str(error)}, status_code=409
        )
    if isinstance(error, (ValueError, json.JSONDecodeError)):
        return JSONResponse(
            {"error": "invalid_request", "message": "request is invalid"}, status_code=400
        )
    if isinstance(error, PermissionError):
        return JSONResponse(
            {"error": "confirmation_required", "message": "operation requires confirmation"},
            status_code=403,
        )
    return JSONResponse(
        {"error": "operation_failed", "message": "operation failed"}, status_code=500
    )


__all__ = ["create_management_app"]
