"""Application service coordinating persisted connections and live adapters."""

from __future__ import annotations

import asyncio
import builtins
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import cast
from uuid import UUID, uuid4
from weakref import WeakValueDictionary

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
    DiagnosticSeverity,
    ProbeResult,
    ValidationResult,
)
from domoai.domain.models import ScalarValue
from domoai.persistence.connection_repository import ConnectionRepository, SecretTransfer
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.identity import runtime_adapter_id
from domoai.runtime.managed_adapter_set import ManagedAdapterSet
from domoai.runtime.operational_metrics import RuntimeOperationalMetrics
from domoai.runtime.ports import AuditSinkPort
from domoai.security.secrets import (
    PreparedSecret,
    SecretResolverPort,
    SecretStorePort,
    TemporarySecretStorePort,
)


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
        self.onboarding = OnboardingCoordinator(providers, close_session=self._close_temporary)
        self._owned_transfers: dict[UUID, tuple[SecretTransfer, PreparedSecret, object]] = {}
        self._inflight_transfers: set[UUID] = set()
        self._operation_locks: WeakValueDictionary[tuple[str, UUID], asyncio.Lock] = (
            WeakValueDictionary()
        )

    def _operation_lock(self, kind: str, identity: UUID) -> asyncio.Lock:
        key = (kind, identity)
        lock = self._operation_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._operation_locks[key] = lock
        return lock

    async def list(self) -> builtins.list[ConnectionProfile]:
        for transfer, prepared, owner in tuple(self._owned_transfers.values()):
            recovered = await self._read_adoption(transfer, prepared, owner)
            if recovered is None and transfer.transfer_id not in self._inflight_transfers:
                if not await self._discard_owned_transfer(transfer, prepared, owner):
                    raise self._recovery_pending()
        return await self.repository.list_all()

    async def get(self, connection_id: UUID) -> ConnectionProfile:
        for transfer, prepared, owner in tuple(self._owned_transfers.values()):
            if transfer.profile_id == connection_id:
                recovered = await self._read_adoption(transfer, prepared, owner)
                if recovered is None:
                    raise self._persistence_pending()
        return await self._require_profile(connection_id)

    async def discover(self, connector_id: str) -> builtins.list[ConnectionCandidate]:
        return await self._provider(connector_id).discover()

    async def start_setup(self, connector_id: str) -> tuple[UUID, tuple[ConnectionCandidate, ...]]:
        return await self.onboarding.start(connector_id)

    async def select_setup(self, session_id: UUID, candidate_id: str) -> ConnectionCandidate:
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
        self._require_borrowed_reference(credential_ref)
        self._close_temporary(session_id)
        stored_ref = (
            self._temporary_store().put_temporary(credential_value, owner=session_id)
            if credential_value is not None
            else None
        )
        try:
            draft = ConnectionDraft(
                connector_id=candidate.connector_id,
                unique_id=candidate.candidate_id,
                endpoint=dict(endpoint),
                options=dict(options),
                credential_ref=stored_ref or credential_ref,
                mapping_ref=mapping_ref,
            )
            return await self.onboarding.setup(session_id, draft)
        except Exception:
            self._close_temporary(session_id)
            raise

    async def cancel_setup(self, session_id: UUID) -> bool:
        return self.onboarding.cancel(session_id)

    async def validate_setup(self, session_id: UUID) -> ValidationResult:
        return await self.onboarding.validate(session_id)

    async def create_setup(self, session_id: UUID, *, activate: bool = True) -> ConnectionProfile:
        async with self._operation_lock("setup", session_id):
            setup = await self.onboarding.get_setup(session_id)
            profile = None
            # A cancelled caller may leave an owned worker/transfer after releasing this lock.
            for transfer, prepared, owner in tuple(self._owned_transfers.values()):
                if owner == session_id:
                    profile = await self._read_adoption(transfer, prepared, owner)
                    if profile is None:
                        raise self._persistence_pending()
                    break
            if profile is None:
                profile = await self.create(setup, activate=False, _temporary_owner=session_id)
            if activate:
                async with self._operation_lock("connection", profile.id):
                    await self._assert_setup_admitted(session_id, profile)
                    return await self._activate_locked(profile.id)
            await self._assert_setup_admitted(session_id, profile)
            return profile

    async def _assert_setup_admitted(self, session_id: UUID, profile: ConnectionProfile) -> None:
        setup = await self.onboarding.get_setup(session_id)
        if setup.draft.credential_ref != profile.credential_ref:
            raise ConnectionManagerError("invalid_credentials", "credential reference changed")

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
        self._record_connection_validation_duration(time.perf_counter() - started, connector_id)
        self._record_connection_state("connected" if result.valid else "degraded", connector_id)
        return result

    async def create(
        self,
        setup: ConnectionSetup,
        *,
        credential_value: str | None = None,
        activate: bool = True,
        _temporary_owner: object | None = None,
    ) -> ConnectionProfile:
        provider = self._provider(setup.draft.connector_id)
        if credential_value is not None and setup.draft.credential_ref is not None:
            raise ConnectionManagerError(
                "invalid_credentials", "provide a credential reference or value, not both"
            )
        owner = _temporary_owner if _temporary_owner is not None else object()
        draft = setup.draft
        temporary: TemporarySecretStorePort | None = None
        prepared: PreparedSecret | None = None
        if _temporary_owner is None:
            self._require_borrowed_reference(draft.credential_ref)
        if credential_value is not None:
            temporary = self._temporary_store()
            reference = temporary.put_temporary(credential_value, owner=owner)
            draft = draft.model_copy(update={"credential_ref": reference})
            setup = setup.model_copy(update={"draft": draft})
        elif (
            draft.credential_ref is not None
            and isinstance(self.secret_store, TemporarySecretStorePort)
            and self.secret_store.is_temporary(draft.credential_ref)
        ):
            temporary = self.secret_store
        transfer: SecretTransfer | None = None
        try:
            if temporary is not None and draft.credential_ref is not None:
                prepared = temporary.prepare_temporary(draft.credential_ref, owner=owner)
            validation = await provider.validate(setup)
            if not validation.valid:
                raise ConnectionManagerError("validation_failed", "connection setup is invalid")
            draft = validation.normalized_draft or setup.draft
            if prepared is not None and draft.credential_ref != prepared.reference:
                raise ConnectionManagerError("invalid_credentials", "credential reference changed")
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
                raise ConnectionManagerError(
                    "duplicate_connection", "connection identity already exists"
                )
            if prepared is not None and temporary is not None:
                # Recheck admission after validation without changing the prepared bytes.
                temporary.prepare_temporary(prepared.reference, owner=owner)
                transfer = SecretTransfer(
                    uuid4(),
                    prepared.reference,
                    profile.id,
                    profile.connector_id,
                    profile.unique_id,
                    prepared.digest,
                )
                self._owned_transfers[transfer.transfer_id] = (transfer, prepared, owner)
                profile = await self._write_secret_transfer(profile, transfer, prepared, owner)
            else:
                await self.repository.save(profile)
        except BaseException:
            # Once a durable operation was admitted, a failure cannot prove
            # rollback. Keep its row/file/pin until a successful serialized read.
            if transfer is None and temporary is not None:
                if prepared is not None:
                    temporary.release_temporary(prepared.reference, owner=owner)
                else:
                    temporary.close_temporary(owner)
            raise
        self._audit("connection_created", profile, result="persisted")
        if not activate:
            return profile
        return await self.activate(profile.id)

    @staticmethod
    def _persistence_pending() -> ConnectionManagerError:
        return ConnectionManagerError(
            "connection_persistence_pending", "connection persistence requires recovery"
        )

    @staticmethod
    def _recovery_pending() -> ConnectionManagerError:
        return ConnectionManagerError(
            "connection_recovery_pending", "connection credential requires recovery"
        )

    async def _read_adoption(
        self,
        transfer: SecretTransfer,
        prepared: PreparedSecret,
        owner: object,
    ) -> ConnectionProfile | None:
        try:
            observed = await self.repository.get_secret_transfer(transfer.transfer_id)
            current = await self.repository.get(transfer.profile_id)
            binding = (
                await self.repository.get_credential_transfer_id(transfer.profile_id)
                if current is not None
                else None
            )
        except Exception:
            raise self._persistence_pending() from None
        if observed is None or observed.state != "adopted":
            return None
        if (
            binding != str(transfer.transfer_id)
            or replace(observed, state="pending") != transfer
            or current is None
            or current.credential_ref != transfer.secret_ref
            or (current.connector_id, current.unique_id)
            != (transfer.connector_id, transfer.unique_id)
            or not self._temporary_store().matches_secret(prepared.reference, prepared.digest)
        ):
            raise ConnectionManagerError(
                "connection_recovery_pending", "connection credential requires recovery"
            )
        self._temporary_store().release_temporary(prepared.reference, owner=owner)
        self._owned_transfers.pop(transfer.transfer_id, None)
        return current

    async def _write_secret_transfer(
        self,
        profile: ConnectionProfile,
        transfer: SecretTransfer,
        prepared: PreparedSecret,
        owner: object,
    ) -> ConnectionProfile:
        self._inflight_transfers.add(transfer.transfer_id)
        try:
            return await self._persist_secret_transfer(profile, transfer, prepared, owner)
        finally:
            self._inflight_transfers.discard(transfer.transfer_id)

    async def _discard_owned_transfer(
        self,
        transfer: SecretTransfer,
        prepared: PreparedSecret,
        owner: object,
    ) -> bool:
        if self._owned_transfers.get(transfer.transfer_id) != (transfer, prepared, owner):
            return False
        temporary = self._temporary_store()
        try:
            observed = await self.repository.get_secret_transfer(transfer.transfer_id)
            if observed is None or replace(observed, state="pending") != transfer:
                return False
            if observed.state != "discarded":
                if not await self.repository.begin_secret_discard(transfer):
                    return False
                if not temporary.discard_prepared(prepared):
                    raise self._recovery_pending()
                if not await self.repository.finish_secret_discard(transfer):
                    raise self._recovery_pending()
        except ConnectionManagerError:
            raise
        except Exception:
            raise self._persistence_pending() from None
        temporary.release_temporary(prepared.reference, owner=owner)
        self._owned_transfers.pop(transfer.transfer_id, None)
        return True

    async def _persist_secret_transfer(
        self,
        profile: ConnectionProfile,
        transfer: SecretTransfer,
        prepared: PreparedSecret,
        owner: object,
    ) -> ConnectionProfile:
        temporary = self._temporary_store()
        try:
            await self.repository.prepare_secret_transfer(transfer)
        except Exception:
            try:
                observed = await self.repository.get_secret_transfer(transfer.transfer_id)
            except Exception:
                raise self._persistence_pending() from None
            if observed != transfer:
                # No file publication was admitted before this successful read.
                if observed is None:
                    temporary.release_temporary(prepared.reference, owner=owner)
                    self._owned_transfers.pop(transfer.transfer_id, None)
                raise
        try:
            # A pin is not permission to begin a new step after cancellation.
            temporary.prepare_temporary(prepared.reference, owner=owner)
            temporary.materialize(prepared)
            await self.repository.adopt_secret_transfer(profile, transfer.transfer_id)
        except Exception:
            recovered = await self._read_adoption(transfer, prepared, owner)
            if recovered is None:
                if not await self._discard_owned_transfer(transfer, prepared, owner):
                    raise self._recovery_pending() from None
                raise
            return recovered
        temporary.release_temporary(prepared.reference, owner=owner)
        self._owned_transfers.pop(transfer.transfer_id, None)
        return profile

    def _has_runtime_child(self, connection_id: UUID) -> bool:
        return any(
            child.adapter_id == runtime_adapter_id(connection_id) for child in self.runtime.adapters
        )

    async def activate(self, connection_id: UUID) -> ConnectionProfile:
        async with self._operation_lock("connection", connection_id):
            return await self._activate_locked(connection_id)

    async def replace(self, connection_id: UUID) -> ConnectionProfile:
        async with self._operation_lock("connection", connection_id):
            return await self._replace_locked(connection_id)

    async def reconfigure(
        self,
        connection_id: UUID,
        *,
        endpoint: Mapping[str, ScalarValue],
        options: Mapping[str, ScalarValue],
        mapping_ref: str | None = None,
    ) -> ConnectionProfile:
        async with self._operation_lock("connection", connection_id):
            return await self._reconfigure_locked(
                connection_id, endpoint=endpoint, options=options, mapping_ref=mapping_ref
            )

    async def disable(self, connection_id: UUID) -> bool:
        async with self._operation_lock("connection", connection_id):
            return await self._disable_locked(connection_id)

    async def delete(self, connection_id: UUID) -> bool:
        async with self._operation_lock("connection", connection_id):
            return await self._delete_locked(connection_id)

    async def reauth(self, connection_id: UUID, credential_value: str) -> ConnectionProfile:
        async with self._operation_lock("connection", connection_id):
            return await self._reauth_locked(connection_id, credential_value)

    async def reauth_ref(self, connection_id: UUID, credential_ref: str) -> ConnectionProfile:
        async with self._operation_lock("connection", connection_id):
            return await self._reauth_ref_locked(connection_id, credential_ref)

    async def repair(self, connection_id: UUID) -> ConnectionProfile:
        async with self._operation_lock("connection", connection_id):
            return await self._repair_locked(connection_id)

    async def _activate_locked(self, connection_id: UUID) -> ConnectionProfile:
        profile = await self._require_profile(connection_id)
        await self._assert_current_credential(profile)
        if self._has_runtime_child(connection_id):
            if not profile.enabled:
                await self.repository.set_enabled(connection_id, True)
            return await self._require_profile(connection_id)
        provider = self._provider(profile.connector_id)
        setup = self._setup_for(profile)
        self._resolve_credential(profile.credential_ref)
        adapter = None
        try:
            adapter = await provider.build_adapter(setup, adapter_id=runtime_adapter_id(profile.id))
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
            if adapter is not None and any(child is adapter for child in self.runtime.adapters):
                await self.runtime.detach(adapter.adapter_id)
            if not self._has_runtime_child(connection_id):
                await self.repository.set_enabled(connection_id, False)
            self._audit("connection_activation_failed", profile, result="disabled")
            raise ConnectionManagerError(
                "activation_failed", "connection activation failed"
            ) from error

    async def _replace_locked(self, connection_id: UUID) -> ConnectionProfile:
        profile = await self._require_profile(connection_id)
        await self._assert_current_credential(profile)
        provider = self._provider(profile.connector_id)
        setup = self._setup_for(profile)
        self._resolve_credential(profile.credential_ref)
        self._record_connection_reconnect_attempt(profile.connector_id)
        try:
            adapter = await provider.build_adapter(setup, adapter_id=runtime_adapter_id(profile.id))
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

    async def _reconfigure_locked(
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
        validation = await self._provider(profile.connector_id).validate(self._setup_for(updated))
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

    async def _disable_locked(self, connection_id: UUID) -> bool:
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

    async def _delete_locked(self, connection_id: UUID) -> bool:
        profile = await self.repository.get(connection_id)
        if profile is None:
            return False
        await self._disable_locked(connection_id)
        deleted = await self.repository.delete(connection_id)
        if deleted:
            self._audit("connection_deleted", profile, result="deleted")
        return deleted

    async def _reauth_locked(self, connection_id: UUID, credential_value: str) -> ConnectionProfile:
        temporary = self._temporary_store()
        profile = await self._require_profile(connection_id)
        owner = object()
        reference = temporary.put_temporary(credential_value, owner=owner)
        prepared = temporary.prepare_temporary(reference, owner=owner)
        updated = profile.model_copy(
            update={
                "credential_ref": reference,
                "updated_at": self.clock.now(),
            }
        )
        transfer = SecretTransfer(
            uuid4(),
            reference,
            connection_id,
            profile.connector_id,
            profile.unique_id,
            prepared.digest,
            operation="reauth",
            expected_old_ref=profile.credential_ref,
        )
        self._owned_transfers[transfer.transfer_id] = (transfer, prepared, owner)
        updated = await self._write_secret_transfer(updated, transfer, prepared, owner)
        result = await self._reauth_runtime_locked(updated)
        self._audit("connection_reauthenticated", result, result="updated")
        return result

    async def _reauth_ref_locked(
        self, connection_id: UUID, credential_ref: str
    ) -> ConnectionProfile:
        """Attach an already stored credential without copying its secret value."""

        self._require_borrowed_reference(credential_ref)
        profile = await self._require_profile(connection_id)
        self._resolve_credential(credential_ref)
        updated = profile.model_copy(
            update={"credential_ref": credential_ref, "updated_at": self.clock.now()}
        )
        try:
            changed = await self.repository.compare_and_set_credential_ref(
                updated, profile.credential_ref
            )
            if not changed:
                raise self._recovery_pending()
        except Exception as error:
            # A queued scoped read resolves a lost CAS response; query failure is unknown.
            try:
                current = await self.repository.get(connection_id)
            except Exception as query_error:
                raise self._persistence_pending() from query_error
            if (
                current is None
                or current.credential_ref != credential_ref
                or (current.connector_id, current.unique_id)
                != (profile.connector_id, profile.unique_id)
            ):
                raise self._recovery_pending() from error
            updated = current
        result = await self._reauth_runtime_locked(updated)
        self._audit("connection_reauthenticated", result, result="updated")
        return result

    async def _reauth_runtime_locked(self, profile: ConnectionProfile) -> ConnectionProfile:
        try:
            if self._has_runtime_child(profile.id):
                return await self._replace_locked(profile.id)
            if profile.enabled:
                return await self._activate_locked(profile.id)
            return profile
        except Exception as error:
            # Persistence is already confirmed. Neither pointer nor secrets roll back.
            raise self._recovery_pending() from error

    async def _repair_locked(self, connection_id: UUID) -> ConnectionProfile:
        if self._has_runtime_child(connection_id):
            return await self._replace_locked(connection_id)
        return await self._activate_locked(connection_id)

    async def historical_recovery_diagnostics(self) -> tuple[ConnectionDiagnostic, ...]:
        """Read scoped unresolved metadata only; suitable for passive HTTP status."""
        try:
            pending = await self.repository.has_unresolved_secret_transfers()
        except Exception:
            return (self._recovery_diagnostic("connection_persistence_pending"),)
        return (self._recovery_diagnostic(),) if pending else ()

    async def recover(self) -> tuple[ConnectionDiagnostic, ...]:
        """Validate current attributions without adopting, unlinking or starting adapters."""
        try:
            profiles = await self.repository.list_all()
        except Exception:
            return (self._recovery_diagnostic("connection_persistence_pending"),)
        diagnostics = list(await self.historical_recovery_diagnostics())
        for profile in profiles:
            diagnostic = await self._credential_diagnostic(profile)
            if diagnostic is not None:
                diagnostics.append(diagnostic)
        return tuple(diagnostics)

    @staticmethod
    def _recovery_diagnostic(code: str = "connection_recovery_pending") -> ConnectionDiagnostic:
        return ConnectionDiagnostic(
            code=code,
            severity=DiagnosticSeverity.WARNING,
            retryable=True,
            message="connection credential requires recovery",
        )

    async def _credential_diagnostic(
        self, profile: ConnectionProfile
    ) -> ConnectionDiagnostic | None:
        try:
            binding = await self.repository.get_credential_transfer_id(profile.id)
            transfer = await self.repository.get_secret_transfer(UUID(binding)) if binding else None
        except ValueError:
            return self._recovery_diagnostic()
        except Exception:
            return self._recovery_diagnostic("connection_persistence_pending")
        if binding is not None:
            if (
                transfer is None
                or transfer.state != "adopted"
                or transfer.profile_id != profile.id
                or transfer.secret_ref != profile.credential_ref
                or (transfer.connector_id, transfer.unique_id)
                != (profile.connector_id, profile.unique_id)
                or not isinstance(self.secret_store, TemporarySecretStorePort)
                or not self.secret_store.matches_secret(
                    transfer.secret_ref, transfer.envelope_digest
                )
            ):
                return self._recovery_diagnostic()
        try:
            self._resolve_credential(profile.credential_ref)
        except Exception:
            return self._recovery_diagnostic()
        return None

    async def _assert_current_credential(self, profile: ConnectionProfile) -> None:
        diagnostic = await self._credential_diagnostic(profile)
        if diagnostic is not None:
            raise ConnectionManagerError(diagnostic.code, diagnostic.message)

    async def diagnose(self, connection_id: UUID) -> tuple[ConnectionDiagnostic, ...]:
        profile = await self._require_profile(connection_id)
        recovery = await self._credential_diagnostic(profile)
        if recovery is not None:
            return (recovery,)
        diagnostics = await self._provider(profile.connector_id).diagnose(self._setup_for(profile))
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
        report = self.legacy_importer.import_settings(self.legacy_settings, environment=environment)
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
            raise ConnectionManagerError("provider_not_found", "connection provider is unavailable")
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
        self._temporary_store()
        store = self.secret_store
        if store is None or not callable(getattr(store, "put", None)):
            raise ConnectionManagerError(
                "secret_store_unavailable", "credential storage is unavailable"
            )
        return cast(SecretStorePort, store).put(value)

    def _temporary_store(self) -> TemporarySecretStorePort:
        if not isinstance(self.secret_store, TemporarySecretStorePort):
            raise ConnectionManagerError(
                "temporary_credentials_unsupported", "temporary credentials are unsupported"
            )
        return self.secret_store

    def _close_temporary(self, owner: object) -> None:
        if isinstance(self.secret_store, TemporarySecretStorePort):
            self.secret_store.close_temporary(owner)

    def _require_borrowed_reference(self, reference: str | None) -> None:
        if (
            reference is not None
            and isinstance(self.secret_store, TemporarySecretStorePort)
            and self.secret_store.is_temporary(reference)
        ):
            raise ConnectionManagerError(
                "invalid_credentials", "credential reference is unavailable"
            )

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
        if reference is None or not reference.startswith("secret:") or self.secret_store is None:
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
