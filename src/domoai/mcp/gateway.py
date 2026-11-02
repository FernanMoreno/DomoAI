"""Network entrypoint for the one shared DomoAI MCP runtime."""

from __future__ import annotations

import asyncio
import signal
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx
import uvicorn
from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp import FastMCP
from starlette.types import Receive, Scope, Send

from domoai.application.authority import AuthorityPolicy
from domoai.application.runtime_factory import RuntimeComposition
from domoai.config.settings import Settings
from domoai.domain.errors import DomainError
from domoai.management.auth import is_resident_consent_request
from domoai.mcp.auth import StaticBearerTokenVerifier
from domoai.mcp.unified_server import (
    UnifiedMcpContext,
    _transport_security,
    create_unified_server,
)
from domoai.runtime.approval_store import (
    OperatorApprovalAssertionProvider,
    OperatorPrincipalProvider,
)


def create_gateway_server(
    context: UnifiedMcpContext,
    settings: Settings,
    *,
    runtime: Any | None = None,
) -> FastMCP:
    """Create the configured network server over an already-built runtime."""

    return create_unified_server(context, settings=settings, runtime=runtime)


@dataclass
class GatewayApplication:
    """Own the network app and the runtime lifecycle as one unit."""

    runtime: RuntimeComposition
    server: FastMCP
    # Set only by build_gateway: the one loaded MCP verifier shared with the
    # management callback, and the permission to compose the resident host.
    client_token_verifier: Any = None
    compose_resident: bool = False
    _app: Any = field(init=False, repr=False)
    _resident_consent: Any = field(default=None, init=False, repr=False)
    _resident_verifier: Any = field(default=None, init=False, repr=False)
    _closing: bool = field(default=False, init=False, repr=False)
    _lifecycle_lock: Any = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        app = self.server.streamable_http_app()
        connection_manager = getattr(self.runtime, "connection_manager", None)
        if connection_manager is not None:
            from domoai.application.household_onboarding import HouseholdOnboardingService
            from domoai.application.state_service import StateService
            from domoai.management.api import create_management_app
            from domoai.mcp.auth import StaticBearerTokenVerifier
            from domoai.mcp.configured import _adapter_ids
            from domoai.mcp.resources import coverage_evidence_snapshot

            auth_verifier = None
            verifier = self.client_token_verifier
            if self.runtime.settings.mcp_client_token_file is not None:
                verifier = verifier or StaticBearerTokenVerifier.from_file(
                    self.runtime.settings.mcp_client_token_file
                )

                async def verify_management_token(token: str) -> AccessToken | None:
                    return await verifier.verify_token(token)

                auth_verifier = verify_management_token
            policy = AuthorityPolicy(
                tenant_id=self.runtime.settings.mcp_tenant_id,
                household_id=self.runtime.settings.mcp_household_id,
            )
            onboarding = None
            if getattr(self.runtime, "registry", None) is not None and getattr(
                self.runtime, "state_store", None
            ) is not None:
                onboarding = HouseholdOnboardingService(
                    registry=self.runtime.registry,
                    state_service=StateService(self.runtime.state_store),
                    manager=connection_manager, authority_policy=policy,
                    coverage_projector=coverage_evidence_snapshot,
                    membership=lambda: _adapter_ids(self.runtime.adapter),
                    readiness=lambda: _onboarding_checks(self.runtime),
                    public_url=self.runtime.settings.mcp_public_url.rstrip("/")
                    + self.runtime.settings.mcp_path,
                )
            resident_consent = self._build_resident_consent(policy, verifier)
            self._resident_consent = resident_consent
            management_app = create_management_app(
                connection_manager,
                ets_importer=getattr(self.runtime, "ets_importer", None),
                knx_secure_credentials=getattr(self.runtime, "knx_secure_credentials", None),
                home_assistant_oauth=getattr(self.runtime, "home_assistant_oauth", None),
                home_assistant_oauth_config=getattr(
                    self.runtime, "home_assistant_oauth_config", None
                ),
                home_assistant_oauth_discovery=getattr(
                    self.runtime, "home_assistant_oauth_discovery", None
                ),
                repair_engine=getattr(self.runtime, "repair_engine", None),
                mqtt_discovery=getattr(self.runtime, "mqtt_discovery", None),
                host=self.runtime.settings.mcp_host,
                max_request_body_size=self.runtime.settings.mcp_max_request_body_size,
                tls_enabled=urlparse(self.runtime.settings.mcp_public_url).scheme == "https",
                auth_verifier=auth_verifier,
                authority_policy=policy,
                household_onboarding=onboarding,
                resident_consent=resident_consent,
            )
            app.mount("", management_app)
            app.add_middleware(_ResidentDispatchMiddleware, management_app=management_app)
        if not self.runtime.settings.mcp_server_sent_events:
            transport_security = _transport_security(self.runtime.settings)
            app.add_middleware(
                _RejectServerSentEventsMiddleware,
                mcp_path=self.runtime.settings.mcp_path,
                allowed_hosts=transport_security.allowed_hosts,
                allowed_origins=transport_security.allowed_origins,
            )
        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def managed_lifespan(starlette_app: Any) -> AsyncIterator[None]:
            async with original_lifespan(starlette_app):
                try:
                    yield
                finally:
                    self._close_resident()
                    # FastMCP's session manager cancels its task group and
                    # clears its transport table during lifespan exit. Close
                    # active transports first so SSE responses release their
                    # server-side memory streams before that table disappears.
                    await self.close_http_sessions()

        app.router.lifespan_context = managed_lifespan
        self._app = app

    def _build_resident_consent(self, policy: AuthorityPolicy, shared_verifier: Any) -> Any:
        """Compose the resident host from proven construction evidence, else stay closed."""

        settings = self.runtime.settings
        if (
            not self.compose_resident
            or settings.resident_token_file is None
            or settings.resident_origin is None
        ):
            return None
        from domoai.management.consent import (
            AbsentCredentialPeer,
            InspectableCredentialPeer,
            ResidentAdmission,
            ResidentConsentAdapter,
            ResidentPeerInventory,
        )
        from domoai.mcp.auth import StaticBearerTokenVerifier

        try:
            resident = StaticBearerTokenVerifier.from_file(settings.resident_token_file)
            self._resident_verifier = resident
            peers: tuple[Any, ...]
            if shared_verifier is None:
                peers = (AbsentCredentialPeer("mcp"), AbsentCredentialPeer("management"))
            else:
                peers = (
                    InspectableCredentialPeer("mcp", shared_verifier),
                    InspectableCredentialPeer("management", shared_verifier),
                )
            admission = ResidentAdmission(
                settings=settings,
                resident_verifier=resident,
                peers=ResidentPeerInventory(
                    frozenset({"mcp", "management", "management-static"}),
                    (*peers, AbsentCredentialPeer("management-static")),
                ),
                authority_policy=policy,
            )
            adapter = ResidentConsentAdapter(
                admission=admission, clock=self.runtime.clock, **self._resident_review_options()
            )
            adapter.ensure_available()
        except (ValueError, OSError, DomainError):
            self._resident_verifier = None
            return None
        return adapter

    def _resident_review_options(self) -> dict[str, Any]:
        """Review source and grant store, only when both can be proven; else reviews stay 503."""

        from domoai.mcp.consent_review_source import RuntimeReviewSource
        from domoai.runtime.approval_store import ApprovalStore

        runtime, settings = self.runtime, self.runtime.settings
        store = getattr(runtime, "approval_store", None)
        plans = getattr(runtime, "plan_repository", None)
        preferences = getattr(runtime, "household_preferences", None)
        if type(store) is not ApprovalStore or not store.durable:
            return {}
        registry = getattr(runtime, "registry", None)
        if plans is None or preferences is None or registry is None:
            return {}
        source = RuntimeReviewSource(
            plans=plans,
            rules=getattr(runtime, "automation_rule_repository", None),
            registry=registry,
            preferences=preferences,
            approval_store=store,
            timezone=settings.solar_timezone,
            tenant_id=settings.mcp_tenant_id,
            household_id=settings.mcp_household_id,
        )
        return {"review_source": source, "approval_store": store}

    @property
    def app(self) -> Any:
        return self._app

    async def start(self) -> None:
        await self.runtime.start()

    def _close_resident(self) -> None:
        if self._resident_consent is not None:
            self._resident_consent.close()

    def reload_credentials(self) -> None:
        """Apply offline token rotate/revoke to the owned verifiers.

        Resident confirmation is closed before any credential file is read and
        stays closed for this gateway instance, whatever the outcome; only a
        new gateway built from valid files reopens it.
        """

        with self._lifecycle_lock:
            if self._closing:
                raise ValueError("gateway is closing; build a new gateway")
            self._close_resident()
            owned: list[Any] = []
            for verifier in (self.client_token_verifier, self._resident_verifier):
                if verifier is not None and all(verifier is not seen for seen in owned):
                    owned.append(verifier)
            for verifier in owned:
                try:
                    verifier.reload()
                except ValueError:
                    raise ValueError("credential reload failed") from None

    async def close(self) -> None:
        with self._lifecycle_lock:
            self._closing = True
            self._close_resident()
        try:
            await self.close_http_sessions()
        finally:
            await self.runtime.close()

    async def close_http_sessions(self) -> None:
        """Terminate active stateful MCP sessions before stopping the runtime."""

        await _terminate_active_http_sessions(self.server)

    @asynccontextmanager
    async def http_client(self) -> AsyncIterator[httpx.AsyncClient]:
        """Provide an in-process client for contract/composition tests."""

        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=self.runtime.settings.mcp_public_url,
        ) as client:
            yield client


def _onboarding_checks(runtime: RuntimeComposition) -> list[Any]:
    from domoai.domain.household_onboarding import OnboardingCheck

    lifecycle = runtime.lifecycle
    lifecycle_ok = lifecycle.started and not lifecycle.closing and all(
        state == "running" for state in lifecycle.required_task_states.values()
    )
    return [
        OnboardingCheck(code="service", status="passed"),
        OnboardingCheck(code="lifecycle", status="passed" if lifecycle_ok else "failed"),
        OnboardingCheck(
            code="ownership", status="passed"
            if runtime.ownership is not None and not runtime.ownership.released else "failed",
        ),
        OnboardingCheck(
            code="connectors", status="passed"
            if runtime.connection_provider_registry is not None
            and runtime.connection_provider_registry.providers else "pending",
        ),
    ]


async def _terminate_active_http_sessions(server: FastMCP) -> None:
    """Terminate stateful MCP sessions before the SDK manager is cancelled.

    MCP 1.x does not expose a public session-manager shutdown method. The
    Streamable HTTP app does expose the manager, and its active transport table
    is the only supported ownership point for sessions created by this server.
    Feature-detect the table and transport method so a future SDK can provide a
    public equivalent without making this lifecycle path fail during startup.
    """
    manager = getattr(server, "session_manager", None)
    instances = getattr(manager, "_server_instances", None)
    if not isinstance(instances, dict):
        return
    for transport in tuple(instances.values()):
        terminate = getattr(transport, "terminate", None)
        if callable(terminate):
            await terminate()


class _ResidentDispatchMiddleware:
    """Route the resident surfaces to management, outside the SDK middleware.

    The surfaces are the three POSTs and the decision-receipt read, decided by
    the one classifier management itself uses for its rate limit.

    Classifies only the ``scope["path"]`` already decoded by the ASGI server;
    scope, headers, client, raw_path, query and body pass through untouched.
    """

    def __init__(self, app: Any, *, management_app: Any) -> None:
        self.app = app
        self.management_app = management_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and is_resident_consent_request(scope["method"], scope["path"]):
            await self.management_app(scope, receive, send)
            return
        await self.app(scope, receive, send)


class _RejectServerSentEventsMiddleware:
    """Keep the MCP GET endpoint compliant without opening an unbounded SSE stream.

    DomoAI replies to client requests; opt-in chat confirmation also sends
    elicitation on the POST response stream. The MCP Streamable HTTP contract
    permits that mode to answer GET with 405. The pinned MCP/sse-starlette
    combination otherwise creates a receive stream for every client GET and
    does not close it on the normal disconnect path. SSE remains an explicit
    deployment opt-in once a dependency version with complete cleanup is
    qualified.
    """

    def __init__(
        self,
        app: Any,
        *,
        mcp_path: str,
        allowed_hosts: list[str],
        allowed_origins: list[str],
    ) -> None:
        self.app = app
        self.mcp_path = mcp_path
        self.allowed_hosts = allowed_hosts
        self.allowed_origins = allowed_origins

    @staticmethod
    def _matches(value: str | None, allowed: list[str], *, wildcard_port: bool = False) -> bool:
        if not value:
            return False
        if value in allowed:
            return True
        if wildcard_port:
            return any(
                item.endswith(":*") and value.startswith(item[:-2] + ":") for item in allowed
            )
        return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "GET" and scope["path"] == self.mcp_path:
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])
            }
            if not self._matches(headers.get("host"), self.allowed_hosts, wildcard_port=True):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 421,
                        "headers": [(b"content-length", b"0")],
                    }
                )
                await send({"type": "http.response.body", "body": b""})
                return
            origin = headers.get("origin")
            if origin is not None and not self._matches(
                origin, self.allowed_origins, wildcard_port=True
            ):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [(b"content-length", b"0")],
                    }
                )
                await send({"type": "http.response.body", "body": b""})
                return
            await send(
                {
                    "type": "http.response.start",
                    "status": 405,
                    "headers": [(b"allow", b"POST, DELETE"), (b"content-length", b"0")],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return
        await self.app(scope, receive, send)


async def _close_gateway_safely(gateway: GatewayApplication) -> None:
    """Finish gateway cleanup before propagating an interrupt cancellation."""

    close_task = asyncio.create_task(gateway.close(), name="domoai-gateway-close")
    interrupted = False
    while not close_task.done():
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            # ``SIGINT`` cancels the task running ``run_gateway``. Repeated
            # interrupts can arrive while cleanup is still running; keep
            # waiting for the independent close task so ownership release,
            # storage closure, and adapter disconnect cannot be skipped.
            interrupted = True

    # Surface a real cleanup failure, but only after the close task has
    # finished. ``result()`` is non-blocking here and also observes exceptions
    # so they are not reported later as an unhandled task failure.
    close_task.result()
    if interrupted:
        raise asyncio.CancelledError


async def build_gateway(
    settings: Settings | None = None,
    *,
    operator_principal_provider: OperatorPrincipalProvider | None = None,
    operator_approval_assertion_provider: OperatorApprovalAssertionProvider | None = None,
    require_configured_adapter: bool = True,
    control_plane_mode: bool = False,
) -> GatewayApplication:
    """Build one configured gateway; callers explicitly own start/close."""

    from domoai.mcp.configured import build_configured_server

    # The deployment entrypoint has no explicit Settings object. In that
    # local-first path an unconfigured installation is the expected setup
    # state; callers supplying Settings retain the strict compatibility
    # default unless they opt into the control plane explicitly.
    if settings is None and not control_plane_mode:
        control_plane_mode = True
        require_configured_adapter = False

    resolved = settings or Settings.from_environment()
    # Load the MCP credential file once, before any runtime effect; MCP,
    # metrics and management then share this exact instance.
    client_verifier = (
        StaticBearerTokenVerifier.from_file(resolved.mcp_client_token_file)
        if resolved.mcp_client_token_file is not None
        else None
    )
    runtime, server = await build_configured_server(
        resolved,
        operator_principal_provider=operator_principal_provider,
        operator_approval_assertion_provider=operator_approval_assertion_provider,
        require_configured_adapter=require_configured_adapter,
        control_plane_mode=control_plane_mode,
        client_token_verifier=client_verifier,
    )
    try:
        return GatewayApplication(
            runtime=runtime,
            server=server,
            client_token_verifier=client_verifier,
            compose_resident=True,
        )
    except BaseException:
        await runtime.close()
        raise


async def run_gateway() -> None:
    # The long-lived shared gateway is a deployment entrypoint. It must never
    # silently expose the deterministic simulator when no real provider is
    # configured. The local stdio entrypoint remains the explicit fixture path.
    gateway = await build_gateway()
    try:
        await gateway.start()
        # Serve the composed app (management, dispatcher, SSE guard), not a
        # fresh SDK app that would bypass them.
        settings = gateway.server.settings
        await uvicorn.Server(
            uvicorn.Config(
                gateway.app,
                host=settings.host,
                port=settings.port,
                log_level=settings.log_level.lower(),
            )
        ).serve()
    finally:
        await _close_gateway_safely(gateway)


def _handle_sigterm(_signum: int, _frame: Any) -> None:
    """Make Uvicorn's post-capture SIGTERM unwind ``run_gateway`` cleanup."""

    raise KeyboardInterrupt


def main() -> None:
    # Uvicorn handles SIGTERM while serving, then re-raises it after restoring
    # the previous handler.  If that previous handler is the OS default, the
    # process exits before ``run_gateway`` reaches its async ``finally`` and
    # the durable runtime owner remains falsely active.  Keeping this handler
    # installed as the previous handler makes the re-raised signal unwind the
    # gateway cleanup path instead.
    previous_handler = signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        try:
            asyncio.run(run_gateway())
        except KeyboardInterrupt:
            # SIGINT/SIGTERM are an expected operator shutdown after the
            # async gateway has released its resources. Do not turn that
            # controlled lifecycle event into a traceback from the launcher.
            return
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
