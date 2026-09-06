"""One general MCP surface for the complete DomoAI runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from domoai.config.settings import Settings
from domoai.mcp.auth import StaticBearerTokenVerifier
from domoai.mcp.compat import ensure_fastmcp_settings_ready
from domoai.mcp.domotics_server import (
    DomoticsMcpContext,
    register_domotics_tools,
)
from domoai.mcp.health import healthz, readyz
from domoai.mcp.ortools_server import (
    OrtoolsMcpContext,
    register_ortools_tools,
)
from domoai.mcp.remote_metrics import MetricsRenderError, render_prometheus_metrics


@dataclass(frozen=True)
class UnifiedMcpContext:
    """Two internal views over one runtime, registry and plan boundary."""

    domotics: DomoticsMcpContext
    optimizer: OrtoolsMcpContext

    def __post_init__(self) -> None:
        if self.domotics.registry is not self.optimizer.registry:
            raise ValueError("unified MCP contexts must share one device registry")
        if self.domotics.facade.plan_service is not self.optimizer.plan_service:
            raise ValueError("unified MCP contexts must share one plan service")


def create_unified_server(
    context: UnifiedMcpContext,
    *,
    settings: Settings | None = None,
    runtime: object | None = None,
) -> FastMCP:
    """Create the sole public MCP server and register every semantic surface."""

    ensure_fastmcp_settings_ready()
    kwargs: dict[str, Any] = {}
    token_verifier: StaticBearerTokenVerifier | None = None
    if settings is not None:
        kwargs.update(
            host=settings.mcp_host,
            port=settings.mcp_port,
            streamable_http_path=settings.mcp_path,
            json_response=settings.mcp_json_response,
            max_request_body_size=settings.mcp_max_request_body_size,
            transport_security=_transport_security(settings),
        )
        if settings.mcp_client_token_file is not None:
            token_verifier = StaticBearerTokenVerifier.from_file(settings.mcp_client_token_file)
            kwargs["token_verifier"] = token_verifier
            kwargs["auth"] = _auth_settings(settings)
        if settings.mcp_metrics_enabled and token_verifier is None:
            raise ValueError("metrics endpoint requires a client token verifier")
    server = FastMCP(
        "DomoAI",
        instructions=(
            "General semantic smart-home operating layer. Discovery, state, "
            "policy, runtime execution and proposal-only OR-Tools optimization "
            "are available through this one MCP connection."
        ),
        **kwargs,
    )
    register_domotics_tools(server, context.domotics)
    register_ortools_tools(server, context.optimizer)
    if runtime is not None:
        server.custom_route("/healthz", methods=["GET"], name="healthz")(healthz)

        async def readiness_route(request: Request) -> JSONResponse:
            return await readyz(runtime, request)

        server.custom_route("/readyz", methods=["GET"], name="readyz")(readiness_route)
    if settings is not None and settings.mcp_metrics_enabled:

        async def metrics_route(request: Request) -> PlainTextResponse:
            if token_verifier is None:
                return _metrics_unavailable_response()
            # The bearer is read directly because this route is outside the
            # MCP SDK's protected transport endpoint. It still uses the exact
            # same hash-only verifier and lifecycle rules.
            bearer = _bearer_from_request(request)
            if bearer is None or await token_verifier.verify_token(bearer) is None:
                return PlainTextResponse(
                    "unauthorized\n",
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            metrics = context.domotics.metrics
            if metrics is None:
                return _metrics_unavailable_response()
            try:
                snapshot = await metrics.snapshot()
                rendered = render_prometheus_metrics(
                    snapshot,
                    max_bytes=settings.mcp_metrics_max_bytes,
                )
            except MetricsRenderError:
                return _metrics_unavailable_response()
            except Exception:  # noqa: BLE001 - fail closed at an edge
                return _metrics_unavailable_response()
            return PlainTextResponse(
                rendered,
                media_type="text/plain; version=0.0.4",
            )

        server.custom_route("/metrics", methods=["GET"], name="metrics")(metrics_route)
    return server


def _bearer_from_request(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, separator, value = header.partition(" ")
    token = value.strip()
    if scheme.lower() != "bearer" or not separator or not token or any(
        character.isspace() for character in token
    ):
        return None
    return token


def _metrics_unavailable_response() -> PlainTextResponse:
    return PlainTextResponse("metrics_unavailable\n", status_code=503)


def _auth_settings(settings: Settings) -> AuthSettings:
    public_url = AnyHttpUrl(settings.mcp_public_url)
    return AuthSettings(
        issuer_url=public_url,
        resource_server_url=public_url,
        required_scopes=[],
    )


def _transport_security(settings: Settings) -> TransportSecuritySettings:
    parsed = urlparse(settings.mcp_public_url)
    public_host = parsed.netloc
    bind_host = f"{settings.mcp_host}:{settings.mcp_port}"
    allowed_hosts = list(dict.fromkeys((public_host, bind_host)))
    public_origin = f"{parsed.scheme}://{parsed.netloc}"
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=[public_origin],
    )
