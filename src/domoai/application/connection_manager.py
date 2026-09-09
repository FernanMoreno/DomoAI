"""Application service coordinating persisted connections and live adapters."""

from __future__ import annotations

import asyncio
import builtins
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import cast
from uuid import UUID, uuid4

from domoai.application.connection_providers import (
    ConnectionProviderPort,
    ConnectionProviderRegistry,
    ConnectionSetup,
    OnboardingCoordinator,
)
from domoai.application.legacy_settings_importer import (
    LegacyImportReport,
    LegacySettingsImporter,
)
from domoai.config.settings import Settings
from domoai.domain.connections import (
    ConnectionCandidate,
    ConnectionDiagnostic,
    ConnectionDraft,
    ConnectionProfile,
    ProbeResult,
    ValidationResult,
)
from domoai.domain.models import ScalarValue
from domoai.persistence.connection_repository import ConnectionRepository
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.identity import runtime_adapter_id
from domoai.runtime.managed_adapter_set import ManagedAdapterSet
from domoai.runtime.operational_metrics import RuntimeOperationalMetrics
from domoai.runtime.ports import AuditSinkPort
from domoai.security.secrets import SecretResolverPort, SecretStorePort


class ConnectionManagerError(RuntimeError):
    """Sanitized application error with a stable operation code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConnectionManager:
    """Coordinate connection profile state, provider onboarding and runtime."""

    def __init__(
        self,
        *,
        repository: ConnectionRepository,
        providers: ConnectionProviderRegistry,
        runtime: ManagedAdapterSet,
        secret_store: SecretStorePort | SecretResolverPort | None = None,
        audit: AuditSinkPort | None = None,
        clock: Clock | None = None,
        refresh_inventory: Callable[[], Awaitable[object] | object] | None = None,
        legacy_settings: Settings | None = None,
        legacy_importer: LegacySettingsImporter | None = None,
        operational_metrics: RuntimeOperationalMetrics | None = None,
    ) -> None:
        self.repository = repository
        self.providers = providers
        self.runtime = runtime
        self.secret_store = secret_store
        self.audit = audit
        self.clock = clock or SystemClock()
        self.refresh_inventory = refresh_inventory
        self.legacy_settings = legacy_settings
        self.operational_metrics = operational_metrics or providers.operational_metrics
        import_secret_store = (
            cast(SecretStorePort, secret_store)
            if callable(getattr(secret_store, "put", None))
            else None
        )
        self.legacy_importer = legacy_importer or LegacySettingsImporter(
            secret_store=import_secret_store
        )
        self.onboarding = OnboardingCoordinator(providers)

    async def list(self) -> builtins.list[ConnectionProfile]:
        return await self.repository.list_all()

    async def get(self, connection_id: UUID) -> ConnectionProfile:
        return await self._require_profile(connection_id)

    async def discover(self, connector_id: str) -> builtins.list[ConnectionCandidate]:
        return await self._provider(connector_id).discover()

    async def start_setup(
        self, connector_id: str
    ) -> tuple[UUID, tuple[ConnectionCandidate, ...]]:
        return await self.onboarding.start(connector_id)

    async def select_setup(
        self, session_id: UUID, candidate_id: str
    ) -> ConnectionCandidate:
        return await self.onboarding.select(session_id, candidate_id)

    async def setup_draft(
        self,
        session_id: UUID,
        *,
        endpoint: Mapping[str, ScalarValue],
        options: Mapping[str, ScalarValue],
        mapping_ref: str | None = None,
        credential_ref: str | None = None,
        credential_value: str | None = None,
    ) -> ConnectionSetup:
        if credential_ref is not None and credential_value is not None:
            raise ConnectionManagerError(
                "invalid_credentials", "provide a credential reference or value, not both"
            )
        candidate = await self.onboarding.get_candidate(session_id)
        stored_ref = self._store_credential(credential_value)
        draft = ConnectionDraft(
            connector_id=candidate.connector_id,
            unique_id=candidate.candidate_id,
            endpoint=dict(endpoint),
            options=dict(options),
            credential_ref=stored_ref or credential_ref,
            mapping_ref=mapping_ref,
        )
        try:
            return await self.onboarding.setup(session_id, draft)
        except Exception:
            self._delete_credential(stored_ref)
            raise

    async def validate_setup(self, session_id: UUID) -> ValidationResult:
        return await self.onboarding.validate(session_id)

    async def create_setup(
        self, session_id: UUID, *, activate: bool = True
    ) -> ConnectionProfile:
        setup = await self.onboarding.get_setup(session_id)
        return await self.create(setup, activate=activate)

    async def probe(self, candidate: ConnectionCandidate) -> ProbeResult:
        return await self._provider(candidate.connector_id).probe(candidate)

    async def validate(self, setup: ConnectionSetup) -> ValidationResult:
        connector_id = setup.draft.connector_id
        started = time.perf_counter()
        try:
            result = await self._provider(connector_id).validate(setup)
        except Exception:
            self._record_connection_state("unavailable", connector_id)
            raise
        self._record_connection_validation_duration(
            time.perf_counter() - started, connector_id
        )
        self._record_connection_state(
            "connected" if result.valid else "degraded", connector_id
        )
        return result

    async def create(
        self,
        setup: ConnectionSetup,
        *,
        credential_value: str | None = None,
        activate: bool = True,
    ) -> ConnectionProfile:
        provider = self._provider(setup.draft.connector_id)
        if credential_value is not None and setup.draft.credential_ref is not None:
            raise ConnectionManagerError(
                "invalid_credentials", "provide a credential reference or value, not both"
            )
        stored_ref = self._store_credential(credential_value)
        draft = setup.draft
        if stored_ref is not None:
            draft = draft.model_copy(update={"credential_ref": stored_ref})
            setup = setup.model_copy(update={"draft": draft})
        try:
            validation = await provider.validate(setup)
        except Exception:
            self._delete_credential(stored_ref)
            raise
        if not validation.valid:
            self._delete_credential(stored_ref)
            raise ConnectionManagerError("validation_failed", "connection setup is invalid")
        draft = validation.normalized_draft or setup.draft
        self._resolve_credential(draft.credential_ref)
        now = self.clock.now()
        profile = ConnectionProfile(
            id=uuid4(),
            connector_id=draft.connector_id,
            unique_id=draft.unique_id,
            endpoint=draft.endpoint,
            options=draft.options,
            credential_ref=draft.credential_ref,
            mapping_ref=draft.mapping_ref,
            enabled=False,
            created_at=now,
            updated_at=now,
        )
        existing = await self.repository.get_by_unique_id(
            profile.connector_id, profile.unique_id
        )
        if existing is not None:
            self._delete_credential(stored_ref)
            raise ConnectionManagerError(
                "duplicate_connection", "connection identity already exists"
            )
        try:
            await self.repository.save(profile)
        except Exception:
            self._delete_credential(stored_ref)
            raise
        self._audit("connection_created", profile, result="persisted")
        if not activate:
            return profile
        return await self.activate(profile.id)

    async def activate(self, connection_id: UUID) -> ConnectionProfile:
        profile = await self._require_profile(connection_id)
        if profile.enabled and any(
            adapter.adapter_id == runtime_adapter_id(connection_id)
            for adapter in self.runtime.adapters
        ):
            return profile
        provider = self._provider(profile.connector_id)
        setup = self._setup_for(profile)
        self._resolve_credential(profile.credential_ref)
        adapter = None
        try:
            adapter = await provider.build_adapter(
                setup, adapter_id=runtime_adapter_id(profile.id)
            )
            await self.runtime.attach(adapter)
            await self.repository.set_enabled(connection_id, True)
            await self._refresh()
            current = await self._require_profile(connection_id)
            self._audit("connection_activated", current, result="active")
            self._record_connection_state("connected", current.connector_id)
            return current
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - manager boundary is sanitized
            self._record_connection_state("unavailable", profile.connector_id)
            if adapter is not None:
                await self.runtime.detach(adapter.adapter_id)
            await self.repository.set_enabled(connection_id, False)
            self._audit("connection_activation_failed", profile, result="disabled")
            raise ConnectionManagerError(
                "activation_failed", "connection activation failed"
            ) from error

    async def replace(self, connection_id: UUID) -> ConnectionProfile:
        profile = await self._require_profile(connection_id)
        provider = self._provider(profile.connector_id)
        setup = self._setup_for(profile)
        self._resolve_credential(profile.credential_ref)
        self._record_connection_reconnect_attempt(profile.connector_id)
        try:
            adapter = await provider.build_adapter(
                setup, adapter_id=runtime_adapter_id(profile.id)
            )
            await self.runtime.replace(runtime_adapter_id(profile.id), adapter)
            await self._refresh()
            self._audit("connection_replaced", profile, result="active")
            self._record_connection_reconnect_success(profile.connector_id)
            self._record_connection_state("connected", profile.connector_id)
            return await self._require_profile(connection_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - manager boundary is sanitized
            self._record_connection_state("unavailable", profile.connector_id)
            self._audit("connection_replace_failed", profile, result="unchanged")
            raise ConnectionManagerError(
                "replace_failed", "connection replacement failed"
            ) from error

    async def reconfigure(
        self,
        connection_id: UUID,
        *,
        endpoint: Mapping[str, ScalarValue],
        options: Mapping[str, ScalarValue],
        mapping_ref: str | None = None,
    ) -> ConnectionProfile:
        """Change connection metadata while preserving its persisted identity."""

        profile = await self._require_profile(connection_id)
        updated = profile.model_copy(
            update={
                "endpoint": dict(endpoint),
                "options": dict(options),
                "mapping_ref": mapping_ref,
                "updated_at": self.clock.now(),
            }
        )
        validation = await self._provider(profile.connector_id).validate(
            self._setup_for(updated)
        )
        if not validation.valid:
            raise ConnectionManagerError("validation_failed", "connection setup is invalid")
        await self.repository.update(updated)
        if not profile.enabled:
            self._audit("connection_reconfigured", updated, result="disabled")
            return updated
        try:
            adapter = await self._provider(profile.connector_id).build_adapter(
                self._setup_for(updated), adapter_id=runtime_adapter_id(connection_id)
            )
            await self.runtime.replace(runtime_adapter_id(connection_id), adapter)
            await self._refresh()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - manager boundary is sanitized
            await self.repository.update(profile)
            self._audit("connection_reconfigure_failed", profile, result="unchanged")
            raise ConnectionManagerError(
                "reconfigure_failed", "connection reconfiguration failed"
            ) from error
        self._audit("connection_reconfigured", updated, result="active")
        return await self._require_profile(connection_id)

    async def disable(self, connection_id: UUID) -> bool:
        profile = await self._require_profile(connection_id)
        changed = profile.enabled
        if changed:
            await self.repository.set_enabled(connection_id, False)
        detached = await self.runtime.detach(runtime_adapter_id(connection_id))
        if changed or detached:
            self._audit("connection_disabled", profile, result="disabled")
            await self._refresh()
            return True
        return False

    async def delete(self, connection_id: UUID) -> bool:
        profile = await self.repository.get(connection_id)
        if profile is None:
            return False
        await self.disable(connection_id)
        deleted = await self.repository.delete(connection_id)
        if deleted:
            self._delete_credential(profile.credential_ref)
            self._audit("connection_deleted", profile, result="deleted")
        return deleted

    async def reauth(self, connection_id: UUID, credential_value: str) -> ConnectionProfile:
        profile = await self._require_profile(connection_id)
        new_ref = self._store_credential(credential_value)
        if new_ref is None:
            raise ConnectionManagerError(
                "secret_store_unavailable", "credential storage is unavailable"
            )
        old_ref = profile.credential_ref
        await self.repository.set_credential_ref(connection_id, new_ref)
        try:
            result = (
                await self.replace(connection_id)
                if profile.enabled
                else await self._require_profile(connection_id)
            )
            if old_ref != new_ref:
                self._delete_credential(old_ref)
            self._audit("connection_reauthenticated", result, result="updated")
            return result
        except Exception:
            self._record_connection_auth_failure(profile.connector_id)
            await self.repository.set_credential_ref(connection_id, old_ref)
            self._delete_credential(new_ref)
            raise

    async def reauth_ref(self, connection_id: UUID, credential_ref: str) -> ConnectionProfile:
        """Attach an already stored credential without copying its secret value."""

        profile = await self._require_profile(connection_id)
        self._resolve_credential(credential_ref)
        old_ref = profile.credential_ref
        await self.repository.set_credential_ref(connection_id, credential_ref)
        try:
            result = (
                await self.replace(connection_id)
                if profile.enabled
                else await self._require_profile(connection_id)
            )
            if old_ref != credential_ref:
                self._delete_credential(old_ref)
            self._audit("connection_reauthenticated", result, result="updated")
            return result
        except Exception:
            await self.repository.set_credential_ref(connection_id, old_ref)
            raise

    async def repair(self, connection_id: UUID) -> ConnectionProfile:
        profile = await self._require_profile(connection_id)
        if profile.enabled:
            return await self.replace(connection_id)
        return await self.activate(connection_id)

    async def diagnose(self, connection_id: UUID) -> tuple[ConnectionDiagnostic, ...]:
        profile = await self._require_profile(connection_id)
        diagnostics = await self._provider(profile.connector_id).diagnose(
            self._setup_for(profile)
        )
        if self.audit is not None:
            self.audit.append(
                event_type="connection_diagnosed",
                actor="connection_manager",
                subject_id=str(profile.id),
                payload={
                    "connector_id": profile.connector_id,
                    "connection_id": str(profile.id),
                    "adapter_id": runtime_adapter_id(profile.id),
                    "result": "diagnostics_returned",
                    "diagnostic_codes": [diagnostic.code for diagnostic in diagnostics],
                },
            )
        return diagnostics

    def _record_connection_state(self, state: str, connector_id: str) -> None:
        if self.operational_metrics is not None:
            self.operational_metrics.record_connection_state(state, connector_id=connector_id)

    def _record_connection_reconnect_attempt(self, connector_id: str) -> None:
        if self.operational_metrics is not None:
            self.operational_metrics.record_connection_reconnect_attempt(connector_id)

    def _record_connection_reconnect_success(self, connector_id: str) -> None:
        if self.operational_metrics is not None:
            self.operational_metrics.record_connection_reconnect_success(connector_id)

    def _record_connection_validation_duration(
        self, elapsed_seconds: float, connector_id: str
    ) -> None:
        if self.operational_metrics is not None:
            self.operational_metrics.record_connection_validation_duration(
                elapsed_seconds, connector_id=connector_id
            )

    def _record_connection_auth_failure(self, connector_id: str) -> None:
        if self.operational_metrics is not None:
            self.operational_metrics.record_connection_auth_failure(connector_id)

    async def import_legacy(
        self, *, environment: Mapping[str, str] | None = None
    ) -> LegacyImportReport:
        """Explicitly import legacy settings as disabled persisted profiles."""

        if self.legacy_settings is None:
            raise ConnectionManagerError(
                "legacy_import_unavailable", "legacy settings are unavailable"
            )
        report = self.legacy_importer.import_settings(
            self.legacy_settings, environment=environment
        )
        persisted: list[ConnectionProfile] = []
        created_refs: list[str] = []
        try:
            for profile in report.profiles:
                existing = await self.repository.get_by_unique_id(
                    profile.connector_id, profile.unique_id
                )
                if existing is not None:
                    persisted.append(existing)
                    continue
                await self.repository.save(profile)
                persisted.append(profile)
                if profile.credential_ref is not None and profile.credential_ref.startswith(
                    "secret:"
                ):
                    created_refs.append(profile.credential_ref)
                self._audit("legacy_connection_imported", profile, result="disabled")
        except Exception:
            for reference in created_refs:
                self._delete_credential(reference)
            raise
        return report.model_copy(update={"profiles": persisted})

    async def _refresh(self) -> None:
        if self.refresh_inventory is None:
            return
        result = self.refresh_inventory()
        if inspect.isawaitable(result):
            await result

    async def _require_profile(self, connection_id: UUID) -> ConnectionProfile:
        profile = await self.repository.get(connection_id)
        if profile is None:
            raise ConnectionManagerError("not_found", "connection was not found")
        return profile

    def _provider(self, connector_id: str) -> ConnectionProviderPort:
        provider = self.providers.get(connector_id)
        if provider is None:
            raise ConnectionManagerError(
                "provider_not_found", "connection provider is unavailable"
            )
        return provider

    @staticmethod
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

    def _store_credential(self, value: str | None) -> str | None:
        if value is None:
            return None
        store = self.secret_store
        if store is None or not callable(getattr(store, "put", None)):
            raise ConnectionManagerError(
                "secret_store_unavailable", "credential storage is unavailable"
            )
        return cast(SecretStorePort, store).put(value)

    def _resolve_credential(self, reference: str | None) -> None:
        if reference is None:
            return
        resolver = self.secret_store
        if resolver is None:
            raise ConnectionManagerError(
                "secret_store_unavailable", "credential resolver is unavailable"
            )
        try:
            resolver.resolve(reference)
        except Exception as error:  # noqa: BLE001 - secret resolver redacts its own errors
            raise ConnectionManagerError(
                "credential_unavailable", "credential is unavailable"
            ) from error

    def _delete_credential(self, reference: str | None) -> None:
        if (
            reference is None
            or not reference.startswith("secret:")
            or self.secret_store is None
        ):
            return
        delete = getattr(self.secret_store, "delete", None)
        if callable(delete):
            delete(reference)

    def _audit(self, event_type: str, profile: ConnectionProfile, *, result: str) -> None:
        if self.audit is None:
            return
        self.audit.append(
            event_type=event_type,
            actor="connection_manager",
            subject_id=str(profile.id),
            payload={
                "connector_id": profile.connector_id,
                "connection_id": str(profile.id),
                "adapter_id": runtime_adapter_id(profile.id),
                "result": result,
            },
        )


__all__ = ["ConnectionManager", "ConnectionManagerError"]
