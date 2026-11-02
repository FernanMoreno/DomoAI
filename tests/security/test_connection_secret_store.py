from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from domoai.security.secrets import (
    FileSecretStore,
    SecretPermissionError,
    SecretReferenceError,
    SecretUnavailableError,
)


def test_put_encrypts_secret_and_resolves_after_restart(tmp_path: Path) -> None:
    store = FileSecretStore(tmp_path / "master.key")
    reference = store.put("ha-token-plaintext")

    assert reference.startswith("secret:")
    assert "ha-token-plaintext" not in repr(store)
    assert all(
        "ha-token-plaintext" not in path.read_text(encoding="utf-8", errors="ignore")
        for path in tmp_path.rglob("*")
        if path.is_file()
    )
    assert FileSecretStore(tmp_path / "master.key").resolve(reference) == "ha-token-plaintext"


def test_secret_files_and_master_key_are_owner_only(tmp_path: Path) -> None:
    reference = FileSecretStore(tmp_path / "master.key").put("value")

    assert stat.S_IMODE((tmp_path / "master.key").stat().st_mode) == 0o600
    assert stat.S_IMODE(next((tmp_path / "secrets").glob("*.json")).stat().st_mode) == 0o600
    assert reference


def test_insecure_existing_master_key_fails_closed(tmp_path: Path) -> None:
    key_path = tmp_path / "master.key"
    key_path.write_bytes(os.urandom(32))
    key_path.chmod(0o644)

    with pytest.raises(SecretPermissionError, match="permissions"):
        FileSecretStore(key_path)


def test_environment_reference_is_resolved_without_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOMOAI_TEST_TOKEN", "environment-secret")
    store = FileSecretStore(tmp_path / "master.key")

    assert store.resolve("env:DOMOAI_TEST_TOKEN") == "environment-secret"
    assert not (tmp_path / "secrets").exists()


def test_missing_or_malformed_references_are_typed_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileSecretStore(tmp_path / "master.key")
    monkeypatch.delenv("DOMOAI_MISSING_TOKEN", raising=False)

    with pytest.raises(SecretUnavailableError) as missing:
        store.resolve("env:DOMOAI_MISSING_TOKEN")
    assert "environment-secret" not in str(missing.value)

    with pytest.raises(SecretReferenceError):
        store.resolve("token:environment-secret")


def test_corrupt_payload_does_not_expose_ciphertext(tmp_path: Path) -> None:
    store = FileSecretStore(tmp_path / "master.key")
    reference = store.put("secret-value")
    payload_path = next((tmp_path / "secrets").glob("*.json"))
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload["ciphertext"] = "not-valid-ciphertext"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SecretUnavailableError) as error:
        store.resolve(reference)
    assert "not-valid-ciphertext" not in str(error.value)
    assert "secret-value" not in str(error.value)


def _scope(household: str = "home-a"):
    from domoai.domain.coordination import LeaseScope

    return LeaseScope(tenant_id="tenant", household_id=household, deployment_id="edge")


def test_scoped_store_keeps_pending_credentials_private_after_restart(tmp_path: Path) -> None:
    owner = FileSecretStore(tmp_path / "master.key", scope=_scope())
    other = FileSecretStore(tmp_path / "master.key", scope=_scope("home-b"))
    reference = owner.put("pending-oauth")
    assert (
        FileSecretStore(tmp_path / "master.key", scope=_scope()).resolve(reference)
        == "pending-oauth"
    )
    with pytest.raises(SecretUnavailableError):
        other.resolve(reference)
    with pytest.raises(SecretUnavailableError):
        other.delete(reference)
    assert owner.resolve(reference) == "pending-oauth"


def test_legacy_and_scoped_secret_formats_cannot_cross_boundaries(tmp_path: Path) -> None:
    legacy = FileSecretStore(tmp_path / "master.key")
    scoped = FileSecretStore(tmp_path / "master.key", scope=_scope())
    legacy_ref, scoped_ref = legacy.put("old"), scoped.put("new")
    for store, reference in ((legacy, scoped_ref), (scoped, legacy_ref)):
        with pytest.raises(SecretUnavailableError):
            store.resolve(reference)
        with pytest.raises(SecretUnavailableError):
            store.delete(reference)
    assert legacy.resolve(legacy_ref) == "old"
    assert scoped.resolve(scoped_ref) == "new"


def test_scoped_delete_authenticates_owner_metadata_before_unlink(tmp_path: Path) -> None:
    owner = FileSecretStore(tmp_path / "master.key", scope=_scope())
    other = FileSecretStore(tmp_path / "master.key", scope=_scope("home-b"))
    reference = owner.put("do-not-delete")
    path = next((tmp_path / "secrets").glob("*.json"))
    payload = json.loads(path.read_text())
    payload["scope"]["household_id"] = "home-b"
    path.write_text(json.dumps(payload))
    with pytest.raises(SecretUnavailableError):
        other.delete(reference)
    assert path.exists()


def test_scoped_environment_uses_only_captured_server_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARBITRARY_ENV_SECRET", "must-not-read")
    bindings = {"DOMOAI_HOME_ASSISTANT_TOKEN": "configured-token"}
    store = FileSecretStore(tmp_path / "master.key", scope=_scope(), environment=bindings)
    bindings["DOMOAI_HOME_ASSISTANT_TOKEN"] = "changed-after-construction"
    assert store.resolve("env:DOMOAI_HOME_ASSISTANT_TOKEN") == "configured-token"
    with pytest.raises(SecretUnavailableError):
        store.resolve("env:ARBITRARY_ENV_SECRET")


def test_secret_adoption_does_not_overwrite_conflicting_destination(tmp_path: Path) -> None:
    import hashlib

    from domoai.security.secrets import SecretStoreError

    legacy = FileSecretStore(tmp_path / "master.key")
    owner = FileSecretStore(tmp_path / "master.key", scope=_scope())
    old_ref, target_ref = legacy.put("legacy-value"), owner.put("existing-value")
    old_path = tmp_path / "secrets" / f"{old_ref[7:]}.json"
    digest = hashlib.sha256(old_path.read_bytes()).hexdigest()
    with pytest.raises(SecretStoreError):
        owner.copy_legacy(old_ref, expected_digest=digest, target_reference=target_ref)
    assert owner.resolve(target_ref) == "existing-value"
    assert legacy.resolve(old_ref) == "legacy-value"


def test_readonly_secret_store_open_never_creates_a_missing_master_key(tmp_path: Path) -> None:
    from domoai.security.secrets import SecretKeyError

    key_path = tmp_path / "unprovisioned" / "master.key"
    with pytest.raises(SecretKeyError):
        FileSecretStore(key_path, scope=_scope(), create_key=False)
    assert not key_path.parent.exists()
