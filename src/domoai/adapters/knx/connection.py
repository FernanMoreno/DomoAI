"""KNX/IP connection onboarding separated from KNX semantic mapping."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path

from domoai.adapters.knx.adapter import KnxAdapter
from domoai.adapters.knx.config import KnxMappingDocument, load_mapping
from domoai.adapters.knx.transport import (
    KnxSecureTransportConfig,
    KnxTransport,
    XknxTransport,
)
from domoai.domain.connections import (
    ConnectionCandidate,
    ConnectionDiagnostic,
    DiagnosticSeverity,
    InventoryStatus,
    ProbeResult,
    ProductStatus,
    SetupFieldDescriptor,
    SetupFieldType,
    SetupStep,
    TransportStatus,
    ValidationResult,
)
from domoai.domain.p2_connections import KnxSecureOptions
from domoai.runtime.connection_ports import (
    ConnectionProviderManifest,
    ConnectionProviderPort,
    ConnectionSetup,
)
from domoai.runtime.ports import AdapterPort
from domoai.security.knx_secure import (
    KnxSecureCredentialStore,
    KnxSecureError,
    knx_secure_diagnostic,
)
from domoai.security.secrets import SecretResolverPort

GatewayDiscoverer = Callable[
    [], Awaitable[Sequence[ConnectionCandidate]] | Sequence[ConnectionCandidate]
]
TransportFactory = Callable[[str, int, bool, dict[str, str]], KnxTransport]
SecureTransportFactory = Callable[
    [str, int, bool, dict[str, str], KnxSecureTransportConfig], KnxTransport
]


class KnxConnectionProvider(ConnectionProviderPort):
    """Onboard KNX tunnelling while leaving mapping as an explicit input."""

    manifest = ConnectionProviderManifest(
        connector_id="knx",
        display_name="KNX/IP",
        protocol="knxnet_ip_tunnelling",
        version="1.0",
        steps=(
            SetupStep(
                step_id="gateway",
                title="KNX/IP gateway",
                input_keys=("host", "port"),
                fields=(
                    SetupFieldDescriptor(
                        key="host", label="Gateway host", input_type=SetupFieldType.TEXT
                    ),
                    SetupFieldDescriptor(
                        key="port", label="Gateway port", input_type=SetupFieldType.NUMBER
                    ),
                ),
            ),
            SetupStep(
                step_id="mapping",
                title="DomoAI mapping",
                input_keys=("mapping_ref",),
                fields=(
                    SetupFieldDescriptor(
                        key="mapping_ref",
                        label="Mapping file",
                        input_type=SetupFieldType.TEXT,
                    ),
                ),
            ),
        ),
    )

    def __init__(
        self,
        *,
        gateway_discoverer: GatewayDiscoverer | None = None,
        transport_factory: TransportFactory | None = None,
        secret_resolver: SecretResolverPort | None = None,
        secure_transport_factory: SecureTransportFactory | None = None,
        secure_defaults: Mapping[str, object] | None = None,
    ) -> None:
        self.gateway_discoverer = gateway_discoverer
        self.secret_resolver = secret_resolver
        self.secure_defaults = dict(secure_defaults or {})
        self.transport_factory = transport_factory or (
            lambda host, port, route_back, group_dpts: XknxTransport(
                host,
                gateway_port=port,
                route_back=route_back,
                group_dpts=group_dpts,
            )
        )
        self.secure_transport_factory = secure_transport_factory or (
            lambda host, port, route_back, group_dpts, secure_config: XknxTransport(
                host,
                gateway_port=port,
                route_back=route_back,
                group_dpts=group_dpts,
                secure_config=secure_config,
            )
        )

    async def discover(self) -> list[ConnectionCandidate]:
        if self.gateway_discoverer is None:
            return []
        result = self.gateway_discoverer()
        if inspect.isawaitable(result):
            result = await result
        candidates = list(result)
        if any(candidate.connector_id != self.manifest.connector_id for candidate in candidates):
            raise ValueError("KNX discovery returned an incompatible candidate")
        return candidates

    def manual_candidate(self, host: str, *, port: int = 3671) -> ConnectionCandidate:
        normalized_host = host.strip()
        if not normalized_host or not 1 <= port <= 65535:
            raise ValueError("KNX gateway endpoint is invalid")
        return ConnectionCandidate(
            connector_id=self.manifest.connector_id,
            candidate_id=f"knx:{normalized_host}:{port}",
            display_name="KNX/IP gateway",
            endpoint={"host": normalized_host, "port": port},
            metadata={"source": "manual", "interface": "tunnelling"},
        )

    async def probe(self, candidate: ConnectionCandidate) -> ProbeResult:
        try:
            _endpoint(candidate.endpoint)
        except ValueError:
            return ProbeResult(
                transport_status=TransportStatus.INCOMPATIBLE,
                inventory_status=InventoryStatus.FAILED,
                product_status=ProductStatus.UNAVAILABLE,
                diagnostics=(
                    _diagnostic("invalid_endpoint", "KNX gateway endpoint is invalid", False),
                ),
            )
        if _metadata_flag(candidate.metadata, "secure_required"):
            return ProbeResult(
                transport_status=TransportStatus.INCOMPATIBLE,
                inventory_status=InventoryStatus.FAILED,
                product_status=ProductStatus.NEEDS_SETUP,
                diagnostics=(
                    _diagnostic(
                        "knx_secure_credentials_required",
                        "KNX gateway requires secure commissioning",
                        False,
                    ),
                ),
            )
        if _metadata_flag(candidate.metadata, "tunnelling") is False:
            return ProbeResult(
                transport_status=TransportStatus.INCOMPATIBLE,
                inventory_status=InventoryStatus.FAILED,
                product_status=ProductStatus.UNAVAILABLE,
                diagnostics=(
                    _diagnostic(
                        "tunnelling_unsupported",
                        "KNX gateway does not expose tunnelling",
                        False,
                    ),
                ),
            )
        if _metadata_flag(candidate.metadata, "tunnel_slot_available") is False:
            return ProbeResult(
                transport_status=TransportStatus.DEGRADED,
                inventory_status=InventoryStatus.FAILED,
                product_status=ProductStatus.UNAVAILABLE,
                diagnostics=(
                    _diagnostic(
                        "knx_no_tunnel_slot",
                        "KNX gateway has no available tunnel slot",
                        True,
                    ),
                ),
            )
        return ProbeResult(
            transport_status=TransportStatus.REACHABLE,
            inventory_status=InventoryStatus.MAPPING_REQUIRED,
            product_status=ProductStatus.NEEDS_SETUP,
        )

    async def validate(self, setup: ConnectionSetup) -> ValidationResult:
        try:
            _endpoint(setup.draft.endpoint)
        except ValueError as error:
            return ValidationResult(
                valid=False,
                diagnostics=(_diagnostic("invalid_endpoint", str(error), False),),
            )
        mapping_ref = setup.draft.mapping_ref
        if mapping_ref is None:
            return ValidationResult(
                valid=False,
                diagnostics=(_diagnostic("mapping_required", "KNX mapping is required", False),),
            )
        try:
            load_mapping(Path(mapping_ref))
        except ValueError:
            return ValidationResult(
                valid=False,
                diagnostics=(_diagnostic("mapping_invalid", "KNX mapping is invalid", False),),
            )
        if (
            setup.draft.options.get("secure") is True
            and "secure_mode" not in setup.draft.options
            and "secure_mode" not in self.secure_defaults
        ):
            return ValidationResult(
                valid=False,
                diagnostics=(
                    _diagnostic(
                        "knx_secure_credentials_required",
                        "KNX secure transport requires explicit secure configuration",
                        False,
                    ),
                ),
            )
        try:
            secure_options = _secure_options(setup, self.secure_defaults)
            if secure_options is not None:
                if self.secret_resolver is None:
                    return ValidationResult(
                        valid=False,
                        diagnostics=(
                            _diagnostic(
                                "knx_secure_credentials_required",
                                "KNX Secure credentials are required",
                                False,
                            ),
                        ),
                    )
                KnxSecureCredentialStore(self.secret_resolver).resolve(
                    secure_options.credential_ref
                )
        except KnxSecureError as error:
            return ValidationResult(
                valid=False,
                diagnostics=(knx_secure_diagnostic(error),),
            )
        except ValueError:
            return ValidationResult(
                valid=False,
                diagnostics=(knx_secure_diagnostic(ValueError("invalid secure options")),),
            )
        normalized_draft = setup.draft
        if secure_options is not None:
            normalized_options = dict(setup.draft.options)
            normalized_options.setdefault("secure_mode", secure_options.mode.value)
            if secure_options.keyring_path is not None:
                normalized_options.setdefault(
                    "secure_keyring_path", secure_options.keyring_path
                )
            if secure_options.interface is not None:
                normalized_options.setdefault("secure_interface", secure_options.interface)
            if secure_options.user_id is not None:
                normalized_options.setdefault("secure_user_id", secure_options.user_id)
            normalized_draft = setup.draft.model_copy(
                update={
                    "options": normalized_options,
                    "credential_ref": (
                        setup.draft.credential_ref or secure_options.credential_ref
                    ),
                }
            )
        return ValidationResult(valid=True, normalized_draft=normalized_draft)

    async def build_adapter(self, setup: ConnectionSetup, *, adapter_id: str) -> AdapterPort:
        validation = await self.validate(setup)
        if not validation.valid or setup.draft.mapping_ref is None:
            raise ValueError("KNX connection validation failed")
        host, port = _endpoint(setup.draft.endpoint)
        mapping = load_mapping(Path(setup.draft.mapping_ref))
        group_dpts = _group_dpts(mapping)
        route_back = setup.draft.options.get("route_back", False)
        if not isinstance(route_back, bool):
            raise ValueError("KNX route_back option is invalid")
        secure_options = _secure_options(setup, self.secure_defaults)
        if secure_options is None:
            transport = self.transport_factory(host, port, route_back, group_dpts)
        else:
            if self.secret_resolver is None:
                raise ValueError("KNX Secure credentials are required")
            credentials = KnxSecureCredentialStore(self.secret_resolver).resolve(
                secure_options.credential_ref
            )
            secure_config = KnxSecureTransportConfig(
                mode=secure_options.mode.value,
                interface=secure_options.interface,
                keyring_path=secure_options.keyring_path,
                keyring_password=credentials.keyring_password,
                user_id=secure_options.user_id,
                user_password=credentials.user_password,
                device_authentication_password=credentials.device_authentication_password,
                backbone_key=credentials.backbone_key,
            )
            transport = self.secure_transport_factory(
                host,
                port,
                route_back,
                group_dpts,
                secure_config,
            )
        return KnxAdapter(
            transport,
            mapping,
            adapter_id=adapter_id,
        )

    async def diagnose(self, setup: ConnectionSetup) -> tuple[ConnectionDiagnostic, ...]:
        return (await self.validate(setup)).diagnostics


def _endpoint(endpoint: Mapping[str, object]) -> tuple[str, int]:
    host = endpoint.get("host")
    port = endpoint.get("port", 3671)
    if not isinstance(host, str) or not host.strip():
        raise ValueError("KNX gateway host is required")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("KNX gateway port is invalid")
    return host.strip(), port


def _metadata_flag(metadata: Mapping[str, object], key: str) -> bool | None:
    value = metadata.get(key)
    return value if isinstance(value, bool) else None


def _group_dpts(mapping: KnxMappingDocument) -> dict[str, str]:
    return {
        binding.state_group_address: binding.dpt
        for entity in mapping.entities
        for binding in entity.capabilities
    }


def _secure_options(
    setup: ConnectionSetup, defaults: Mapping[str, object] | None = None
) -> KnxSecureOptions | None:
    options = dict(defaults or {})
    options.update(setup.draft.options)
    mode = options.get("secure_mode")
    if mode is None:
        if options.get("secure") is True:
            raise KnxSecureError("legacy secure option requires KNX Secure configuration")
        return None
    try:
        return KnxSecureOptions.model_validate(
            {
                "mode": mode,
                "keyring_path": options.get("secure_keyring_path"),
                "interface": options.get("secure_interface"),
                "user_id": options.get("secure_user_id"),
                "credential_ref": setup.draft.credential_ref or options.get("credential_ref"),
            }
        )
    except Exception as error:  # noqa: BLE001 - caller emits sanitized diagnostics
        raise KnxSecureError("KNX Secure configuration is invalid") from error


def _diagnostic(code: str, message: str, retryable: bool) -> ConnectionDiagnostic:
    return ConnectionDiagnostic(
        code=code,
        severity=DiagnosticSeverity.ERROR,
        retryable=retryable,
        message=message,
    )


__all__ = ["KnxConnectionProvider"]
